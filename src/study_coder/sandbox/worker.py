"""容器内固定入口。stdin/stdout 是 JSON 协议，命令输出只进入结果字段。"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from study_coder.config import Settings
from study_coder.sandbox.base import SANDBOX_TOOLS
from study_coder.sandbox.transfer import runtime_export_issues
from study_coder.tools.background import BACKGROUND_TOOLS
from study_coder.tools.base import ToolContext, ToolFailure, resolve_workspace_path, text_arg
from study_coder.tools.edit import EditFileTool, WriteFileTool
from study_coder.tools.read import ReadFileTool
from study_coder.tools.search import GlobTool, GrepTool
from study_coder.tools.skillload import skill_loader
from study_coder.tools.task import (
    claim_task,
    complete_task,
    create_task,
    delete_task,
    get_task,
    reset_task_progress,
    task_list,
    update_task_dependencies,
)
from study_coder.types import ToolResult

WORKSPACE = Path("/workspace")
# 每次 docker exec 启动一个 Worker；文件工具共用容器卷里的 /workspace。
# 常驻服务记录放在容器 /tmp，让后续 Worker 能按 service_id 找到它。
_SERVICE_DIR = Path("/tmp/study-coder-services")
_FILE_TOOLS = {tool.name: tool for tool in
               (ReadFileTool(), GlobTool(), GrepTool(), EditFileTool(), WriteFileTool(),
                skill_loader(), claim_task(), complete_task(), task_list(), get_task(),
                create_task(), update_task_dependencies(), delete_task(),reset_task_progress())}


def _enable_child_subreaper() -> None:
    """让脱离进程组的孙进程在命令退出后仍归 Worker 管理。"""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    except (AttributeError, OSError):
        pass


def _descendants_of(pid: int) -> list[int]:
    """扫描 Linux /proc 的 PPID 关系，返回某进程的全部后代 PID。"""
    parent_map: dict[int, int] = {}
    try:
        entries = os.scandir("/proc")
    except OSError:
        return []
    with entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                raw = Path(entry.path, "stat").read_text(encoding="ascii")
                fields = raw[raw.rfind(")") + 2:].split()
                parent_map[int(entry.name)] = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
    found: set[int] = set()
    frontier = [pid]
    while frontier:
        parent = frontier.pop()
        children = [child for child, ppid in parent_map.items()
                    if ppid == parent and child not in found]
        found.update(children)
        frontier.extend(children)
    return list(found)


def _stop_command_descendants() -> None:
    """每次调用结束都清理其派生进程，避免后台命令继续改工作卷。"""
    for _ in range(3):
        descendants = _descendants_of(os.getpid())
        if not descendants:
            break
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        for child in descendants:
            try:
                os.kill(child, sigkill)
            except ProcessLookupError:
                pass
        for _ in descendants:
            try:
                os.waitpid(-1, getattr(os, "WNOHANG", 1))
            except ChildProcessError:
                break
        time.sleep(0.02)


def _workspace_usage(max_bytes: int | None = None,
                     max_files: int | None = None) -> tuple[int, int]:
    """返回 /workspace 的逻辑字节数和文件数，可在达到上限后提前返回。"""
    total = files = 0
    for base, dirs, names in os.walk(WORKSPACE, followlinks=False):
        current = Path(base)
        for name in dirs:
            try:
                if (current / name).is_symlink():
                    files += 1
                    total += (current / name).lstat().st_size
                    if ((max_bytes is not None and total > max_bytes)
                            or (max_files is not None and files > max_files)):
                        return total, files
            except OSError:
                continue
        for name in names:
            try:
                info = (current / name).lstat()
                files += 1
                total += info.st_size
                if ((max_bytes is not None and total > max_bytes)
                        or (max_files is not None and files > max_files)):
                    return total, files
            except OSError:
                continue
    return total, files


def _would_exceed_file_budget(name: str, arguments: dict[str, Any],
                              total: int, files: int,
                              max_bytes: int, max_files: int) -> bool:
    """估算 write/edit 后的用量，超限返回 True。

    文件工具写入前先检查；无法解析的参数留给具体工具报告错误。
    bash 无法静态预测写入量，需由 _shell 的监控线程周期扫描。
    """
    try:
        path = resolve_workspace_path(WORKSPACE, text_arg(arguments, "path"))
        exists = path.is_file()
        old_size = path.stat().st_size if exists else 0
        if name == "write_file":
            content = text_arg(arguments, "content", allow_empty=True)
            new_size = len(content.encode("utf-8"))
            new_files = files + (0 if exists else 1)
        elif name == "edit_file" and exists:
            before = path.read_bytes().decode("utf-8")
            old_text = text_arg(arguments, "old_text")
            new_text = text_arg(arguments, "new_text", allow_empty=True)
            if before.count(old_text) != 1:
                return False
            new_size = len(before.replace(old_text, new_text, 1).encode("utf-8"))
            new_files = files
        else:
            return False
        return total - old_size + new_size > max_bytes or new_files > max_files
    except (ToolFailure, OSError, UnicodeError, ValueError):
        return False


def _process_group_alive(group_id: int) -> bool:
    """只把仍能运行的组成员视为存活；PID 1 可能暂不回收孤儿僵尸进程。"""
    try:
        entries = os.scandir("/proc")
    except OSError:
        entries = None
    if entries is not None:
        unreadable = False
        with entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    raw = Path(entry.path, "stat").read_text(encoding="ascii")
                    fields = raw[raw.rfind(")") + 2:].split()
                    if int(fields[2]) == group_id and fields[0] not in {"Z", "X"}:
                        return True
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, IndexError):
                    unreadable = True
        if not unreadable:
            return False
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        try:
            os.kill(group_id, 0)
        except ProcessLookupError:
            return False
        return True
    try:
        killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_identity(pid: int) -> tuple[int, str, str] | None:
    """读取 Linux 进程组和启动时钟，避免旧 service_id 命中复用的 PID。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[2]), fields[19], fields[0]
    except (OSError, ValueError, IndexError):
        return None


def _stop_service_group(group_id: int) -> None:
    """先给服务进程组 SIGTERM，超时再 SIGKILL，并确认可运行成员退出。"""
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise ToolFailure("service lifecycle is supported only in Linux containers")
    if not _process_group_alive(group_id):
        return
    killpg(group_id, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _process_group_alive(group_id):
        time.sleep(0.1)
    if _process_group_alive(group_id):
        killpg(group_id, getattr(signal, "SIGKILL", signal.SIGTERM))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _process_group_alive(group_id):
            time.sleep(0.1)
    if _process_group_alive(group_id):
        raise ToolFailure("service process group could not be stopped")


def _start_service(arguments: dict[str, Any], allowed_ports: tuple[int, ...],
                   workspace_max_bytes: int, workspace_max_files: int) -> ToolResult:
    """启动常驻服务并等端口就绪，成功返回 service_id 与本机预览地址。

    端口必须来自宿主策略；独立的 service_runner 在本次 Worker 退出后继续运行。
    PID 与启动时间会写到 /tmp 记录，供后续 stop_service 核对。
    """
    command = text_arg(arguments, "command")
    port = arguments.get("port")
    if type(port) is not int or port not in allowed_ports:
        raise ToolFailure("port is not enabled in the sandbox service policy")
    runner_config = json.dumps({"command": command, "workspace": str(WORKSPACE),
                                "workspace_max_bytes": workspace_max_bytes,
                                "workspace_max_files": workspace_max_files},
                               ensure_ascii=False)
    if len(runner_config.encode("utf-8")) > 100_000:
        raise ToolFailure("service command exceeds 100 KiB")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            raise ToolFailure(f"port {port} is already in use")
    except OSError:
        pass
    _SERVICE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    service_id = uuid.uuid4().hex
    process = subprocess.Popen(["/opt/study-coder/.venv/bin/python", "-I","-B", "-m","study_coder.sandbox.service_runner",
                                runner_config], cwd=WORKSPACE,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               close_fds=True)
    identity = _process_identity(process.pid)
    # 新进程必须是自己的进程组长，否则后续不能安全地按组停止服务。
    if identity is None or identity[0] != process.pid:
        try:
            _stop_service_group(process.pid)
        except (OSError, ToolFailure):
            pass
        raise ToolFailure("service supervisor could not be identified")
    ready = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None or not _process_group_alive(process.pid):
            break
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                ready = True
                break
        except OSError:
            time.sleep(0.2)
    if not ready:
        try:
            _stop_service_group(process.pid)
        except (OSError, ToolFailure):
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise ToolFailure(f"service did not listen on port {port} within 20 seconds")
    latest_identity = _process_identity(process.pid)
    if (process.poll() is not None or latest_identity is None
            or latest_identity[:2] != identity[:2]
            or latest_identity[2] in {"Z", "X"}):
        try:
            _stop_service_group(process.pid)
        except (OSError, ToolFailure):
            pass
        raise ToolFailure(f"service on port {port} exited during startup")
    record = {"service_id": service_id, "pid": process.pid,
              "process_group": process.pid, "process_start_time": identity[1],
              "port": port}
    record_path = _SERVICE_DIR / f"{service_id}.json"
    # 服务实际就绪后才发布记录，避免返回一个无法停止的半成品 service_id。
    temporary = _SERVICE_DIR / f".{service_id}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(json.dumps(record))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, record_path)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        try:
            _stop_service_group(process.pid)
            process.wait(timeout=5)
        except (OSError, ToolFailure, subprocess.TimeoutExpired):
            pass
        raise ToolFailure("could not save the service record") from exc
    return ToolResult("", "start_service",
                      f"service_id: {service_id}; preview: http://127.0.0.1:{port}")


def _stop_service(arguments: dict[str, Any]) -> ToolResult:
    """按 service_id 停止服务；先核对 PID 启动时间以避免误杀复用 PID。"""
    service_id = text_arg(arguments, "service_id")
    if not re.fullmatch(r"[0-9a-f]{32}", service_id):
        raise ToolFailure("invalid service_id")
    record_path = _SERVICE_DIR / f"{service_id}.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        pid = record["pid"]
        group_id = record["process_group"]
        start_time = record["process_start_time"]
        if (type(pid) is not int or pid <= 1 or type(group_id) is not int
                or group_id != pid or not isinstance(start_time, str)):
            raise ValueError("invalid process group")
    except FileNotFoundError as exc:
        raise ToolFailure("service_id was not found") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolFailure("service record is invalid") from exc
    identity = _process_identity(pid)
    if identity is None or identity[1] != start_time or identity[0] != group_id:
        record_path.unlink(missing_ok=True)
        return ToolResult("", "stop_service", f"service {service_id} was already stopped")
    if identity[2] in {"Z", "X"}:
        record_path.unlink(missing_ok=True)
        return ToolResult("", "stop_service", f"service {service_id} was already stopped")
    _stop_service_group(group_id)
    record_path.unlink(missing_ok=True)
    return ToolResult("", "stop_service", f"service {service_id} stopped")


def _shell(arguments: dict[str, Any], timeout_s: int, output_bytes: int,
           workspace_max_bytes: int, workspace_max_files: int) -> ToolResult:
    """在容器 /workspace 运行一条 shell 命令并返回状态、输出与耗时。

    这里明确调用容器内 /bin/sh -c，支持管道和重定向；宿主 Docker 控制命令
    仍由固定 argv 执行。进程组/后代清理、输出字节预算和逻辑工作区预算均
    限制这一次命令；timeout_s 来自宿主已经批准的请求。
    """
    command = text_arg(arguments, "command")
    start = time.monotonic()
    _enable_child_subreaper()
    process = subprocess.Popen(["/bin/sh", "-c", command], cwd=WORKSPACE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = threading.Event()
    lock = threading.Lock()
    over_quota = threading.Event()
    stop_monitor = threading.Event()

    def collect(name: str, pipe: Any) -> None:
        # 两个管道并发排空，避免其中一条写满后把子进程堵住。
        while data := pipe.read(65_536):
            with lock:
                remaining = output_bytes - sum(len(value) for value in chunks.values())
                if remaining > 0:
                    chunks[name].extend(data[:remaining])
                if len(data) > remaining:
                    truncated.set()
        pipe.close()

    assert process.stdout is not None and process.stderr is not None
    readers = [threading.Thread(target=collect, args=("stdout", process.stdout), daemon=True),
               threading.Thread(target=collect, args=("stderr", process.stderr), daemon=True)]
    for reader in readers:
        reader.start()

    def monitor_workspace() -> None:
        # bash 能运行任意开发程序，写入量只能运行中周期扫描；不是 volume 硬配额。
        while not stop_monitor.wait(0.5):
            size, count = _workspace_usage(workspace_max_bytes, workspace_max_files)
            if size > workspace_max_bytes or count > workspace_max_files:
                over_quota.set()
                try:
                    killpg = getattr(os, "killpg", None)
                    if killpg is not None:
                        killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                return

    monitor = threading.Thread(target=monitor_workspace, daemon=True)
    monitor.start()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # shell 可派生子进程；只杀直接 PID 会留下后台命令。
        try:
            killpg = getattr(os, "killpg", None)
            if killpg is not None:
                killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()
        stop_monitor.set()
        monitor.join(timeout=2)
        _stop_command_descendants()
        for reader in readers:
            reader.join(timeout=2)
        return ToolResult("", "bash", f"Error: command timed out after {timeout_s}s", True)
    finally:
        stop_monitor.set()
        monitor.join(timeout=2)
        # 命令退出也要收尾：后台子进程不能在下一次工具调用后继续写 volume。
        _stop_command_descendants()
    for reader in readers:
        reader.join(timeout=2)
    if over_quota.is_set():
        return ToolResult("", "bash", "Error: sandbox workspace size or file limit exceeded", True)
    stdout = chunks["stdout"].decode("utf-8", errors="replace")
    stderr = chunks["stderr"].decode("utf-8", errors="replace")
    note = "\n[command output truncated]" if truncated.is_set() else ""
    message = (f"exit code: {process.returncode}; elapsed: {time.monotonic() - start:.2f}s\n"
               f"stdout:\n{stdout}\nstderr:\n{stderr}{note}")
    # 短命令可能在 0.5 秒监测间隔内完成，所以退出后仍检查交付条件。
    # 不删除阻塞文件：模型可继续用 bash 修复，用户也可显式追加导出排除规则。
    issues = runtime_export_issues(WORKSPACE, workspace_max_bytes, workspace_max_files)
    if issues:
        message += "\n[export preflight failed]\n" + "\n".join(issues)
    return ToolResult("", "bash", message, process.returncode != 0 or bool(issues))


def execute(request: dict[str, Any]) -> ToolResult:
    """验证一次 JSON 请求并分派工具，返回给宿主的 ToolResult。

    Worker 的检查保护协议和容器内实现；宿主 execute_tool 仍负责最终策略
    上限。文件工具通过 ToolContext 指向同一 /workspace 副本。
    """
    name = request.get("name")
    arguments = request.get("arguments")
    if name not in SANDBOX_TOOLS or not isinstance(arguments, dict):
        return ToolResult("", str(name), "Error: invalid sandbox tool request", True)
    timeout_s = request.get("timeout_s", 60)
    output_bytes = request.get("output_bytes", 1_048_576)
    max_chars = request.get("max_tool_output_chars", 12_000)
    max_skill_chars = request.get("max_skill_chars", 40_000)
    if type(max_skill_chars) is not int or not 1 <= max_skill_chars <= 1_000_000:
        return ToolResult("", name, "Error: invalid max_skill_chars", True)
    workspace_max_bytes = request.get("workspace_max_bytes", 1_073_741_824)
    workspace_max_files = request.get("workspace_max_files", 50_000)
    raw_service_ports = request.get("service_ports", [])
    if (type(timeout_s) is not int or not 1 <= timeout_s <= 86_400
            or type(output_bytes) is not int or not 4096 <= output_bytes <= 8_388_608
            or type(max_chars) is not int or not 1 <= max_chars <= 1_000_000
            or type(workspace_max_bytes) is not int
            or not 128 * 1024 * 1024 <= workspace_max_bytes <= 64 * 1024**3
            or type(workspace_max_files) is not int
            or not 10_000 <= workspace_max_files <= 1_000_000
            or not isinstance(raw_service_ports, list)
            or len(raw_service_ports) > 10
            or any(type(port) is not int or not 1024 <= port <= 65535
                   for port in raw_service_ports)):
        return ToolResult("", name, "Error: invalid sandbox resource limit", True)
    service_ports = tuple(raw_service_ports)
    if name in BACKGROUND_TOOLS:
        return ToolResult("", name, "Error: background tools require the dedicated Docker control route", True)
    if name == "start_service":
        try:
            return _start_service(arguments, service_ports,
                                  workspace_max_bytes, workspace_max_files)
        except (ToolFailure, OSError) as exc:
            return ToolResult("", name, f"Error: {exc}", True)
    if name == "stop_service":
        try:
            return _stop_service(arguments)
        except (ToolFailure, OSError) as exc:
            return ToolResult("", name, f"Error: {exc}", True)
    if name == "bash":
        return _shell(arguments, timeout_s, output_bytes,
                      workspace_max_bytes, workspace_max_files)
    current_size, current_files = _workspace_usage(workspace_max_bytes, workspace_max_files)
    if current_size > workspace_max_bytes or current_files > workspace_max_files:
        return ToolResult("", name, "Error: sandbox workspace size or file limit exceeded", True)
    if _would_exceed_file_budget(name, arguments, current_size, current_files,
                                 workspace_max_bytes, workspace_max_files):
        return ToolResult("", name, "Error: file operation would exceed sandbox workspace limit",
                          True)
    settings = Settings("sandbox", None, None, WORKSPACE,
                        command_timeout_s=timeout_s, max_tool_output_chars=max_chars,
                        max_skill_chars=max_skill_chars)
    context = ToolContext(WORKSPACE, WORKSPACE, settings)
    result = _FILE_TOOLS[name].execute(arguments, context)
    final_size, final_files = _workspace_usage(workspace_max_bytes, workspace_max_files)
    if final_size > workspace_max_bytes or final_files > workspace_max_files:
        return ToolResult("", name, "Error: sandbox workspace size or file limit exceeded", True)
    if name in {"write_file", "edit_file"} and not result.is_error:
        issues = runtime_export_issues(WORKSPACE, workspace_max_bytes, workspace_max_files)
        if issues:
            return ToolResult("", name, result.content + "\n[export preflight failed]\n"
                              + "\n".join(issues), True)
    return result


def main() -> int:
    """从 stdin 读取最多 1 MiB JSON，stdout 只输出一份机器可解析结果。"""
    try:
        payload = sys.stdin.buffer.read(1_048_577)
        if len(payload) > 1_048_576:
            raise ToolFailure("sandbox request exceeds 1 MiB")
        request = json.loads(payload)
        if not isinstance(request, dict):
            raise ToolFailure("sandbox request must be an object")
        result = execute(request)
    except (ToolFailure, ValueError, OSError) as exc:
        result = ToolResult("", "sandbox", f"Error: {exc}", True)
    except Exception:
        result = ToolResult("", "sandbox", "Internal sandbox worker error", True)
    sys.stdout.write(json.dumps({"name": result.name, "content": result.content,
                                 "is_error": result.is_error}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
