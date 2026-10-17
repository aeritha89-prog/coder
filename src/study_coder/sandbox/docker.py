"""Docker 后端：隔离工作卷、固定 Worker、容器级取消和受审查的导出。"""

from __future__ import annotations

import difflib
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO, cast

from study_coder.config import Settings
from study_coder.sandbox.background import BackgroundManager
from study_coder.sandbox.base import SANDBOX_TOOLS, SandboxError
from study_coder.sandbox.manifest import load_record, save_record, task_dir
from study_coder.sandbox.policy import SandboxPolicy
from study_coder.sandbox.transfer import (
    apply_changes,
    changes,
    create_import_archive,
    extract_export,
    filter_snapshot,
    recover_apply,
    tar_exclude_patterns,
)
from study_coder.tools.background import BACKGROUND_TOOLS
from study_coder.tools.skillload import skill_loader
from study_coder.tools.task import TASK_STATE_MAX_CHARS
from study_coder.types import ToolResult

_EXPORT_DIR_NAME = re.compile(r"export-[0-9a-f]{32}")
_APPLY_JOURNAL_NAME = re.compile(r"apply-[0-9a-f]{32}\.json")
_APPLY_DIR_NAME = re.compile(r"apply-[0-9a-f]{32}")


def _task_artifact(directory: Path, name: object, pattern: re.Pattern[str],
                   description: str) -> Path:
    """把记录里的工件名约束在任务目录内，防止记录内容变成任意宿主路径。"""
    if not isinstance(name, str) or not pattern.fullmatch(name):
        raise SandboxError(f"sandbox {description} path is invalid")
    path = directory / name
    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
        raise SandboxError(f"sandbox {description} path is invalid")
    return path


def _format_export_failure(exc: Exception, returncode: int | None,
                           stderr: bytes) -> SandboxError:
    """保留 Python 校验异常，同时附上有用的 exporter 状态与 stderr。"""
    detail = stderr.decode("utf-8", errors="replace")[:500].strip()
    if isinstance(exc, SandboxError):
        if detail and detail not in str(exc):
            return SandboxError(f"{exc} (exporter stderr: {detail})")
        return exc
    if isinstance(exc, subprocess.TimeoutExpired):
        # 此处 returncode 是收尾时 kill 出来的，不是导出容器自己的退出码，
        # 不能拿它当作 Docker 或 tar 失败的证据。
        return SandboxError(
            f"Docker exporter did not exit in time (stderr: {detail or 'empty'}): {exc}")
    if returncode:
        return SandboxError(
            f"Docker export failed (exit {returncode}; "
            f"{detail or 'no stderr output'}); export validation failed: {exc}")
    return SandboxError(f"Docker export could not be verified: {exc}")


def _remove_owned_resource(kind: str, name: str, task_id: str) -> None:
    """只删除具有本任务标签的容器、网络或 volume；不存在视作已清理。

    任务记录提供名称，但名称不能证明归属；inspect 的标签必须与 task_id 一致。
    """
    if kind == "container":
        template = '{{ index .Config.Labels "study-coder.task" }}'
    elif kind == "volume":
        template = '{{ index .Labels "study-coder.task" }}'
    elif kind == "network":
        template = '{{ index .Labels "study-coder.task" }}'
    else:
        raise ValueError("unsupported Docker resource kind")
    try:
        owner = _control([kind, "inspect", "--format", template, name], timeout=15)
    except SandboxError as exc:
        if "no such" in str(exc).casefold() or "not found" in str(exc).casefold():
            return
        raise
    if owner != task_id:
        raise SandboxError(f"refusing to remove unowned Docker {kind}: {name}")
    args = ([kind, "rm", "-f", name] if kind == "container"
            else [kind, "rm", name])
    _control(args, timeout=20)


def _docker_executable() -> str:
    """定位宿主 Docker CLI；失败时不会退回本地工作区执行。"""
    executable = shutil.which("docker")
    if not executable:
        raise SandboxError("Docker CLI is not installed or not on PATH")
    return executable


def _control(args: list[str], *, timeout: int = 120,
             stdin_file: Path | None = None) -> str:
    """运行一条宿主 Docker 管理命令，返回去掉首尾空白的 stdout。

    args 是独立参数数组，可选 stdin_file 直接喂给 Docker；subprocess.run
    不经宿主 shell 解释模型命令。非零退出与超时统一转成 SandboxError。
    """
    command = [_docker_executable(), *args]
    try:
        if stdin_file is None:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        else:
            with stdin_file.open("rb") as input_stream:
                result = subprocess.run(command, stdin=input_stream, capture_output=True,
                                        timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(f"Docker command failed or timed out: {args[0]}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")[:1000].strip()
        raise SandboxError(f"Docker {args[0]} failed: {detail or result.returncode}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def doctor(policy: SandboxPolicy) -> dict[str, str]:
    """预检 Docker daemon、镜像和 Worker 导入，返回固定 image ID 等信息。

    任务启动使用返回的不可变镜像 ID，避免预检之后同名镜像标签被重新指向。
    预览端口与 allowlist 网络要求支持 isolated gateway 的 Docker Engine。
    """
    executable = _docker_executable()
    os_type = _control(["info", "--format", "{{.OSType}}"], timeout=15)
    if os_type != "linux":
        raise SandboxError("Docker must be running Linux containers")
    engine_version = ""
    if policy.service_ports or policy.network == "allowlist":
        engine_version = _control(["version", "--format", "{{.Server.Version}}"], timeout=15)
        match = re.match(r"^(\d+)\.(\d+)", engine_version)
        if not match or tuple(map(int, match.groups())) < (28, 0):
            raise SandboxError("service ports and allowlist networking require Docker Engine 28+")
    image_id = _control(["image", "inspect", policy.image, "--format", "{{.Id}}"], timeout=15)
    if not image_id.startswith("sha256:"):
        raise SandboxError("sandbox image inspection returned no immutable image ID")
    _control(["run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
              "--security-opt", "no-new-privileges", "--user", "10001:10001",
              "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", image_id,
              "python", "-c", "import study_coder.sandbox.worker, "
              "study_coder.sandbox.network_proxy, study_coder.sandbox.service_runner, "
              "study_coder.sandbox.background_runner"],
             timeout=30)
    result = {"docker": executable, "os": os_type, "image": policy.image,
              "image_id": image_id, "network": policy.network}
    if engine_version:
        result["engine_version"] = engine_version
    return result


def _image_skill_catalog(image_id: str) -> dict[str, dict[str, str]]:
    """从固定 image ID 读取可信资产；宿主 schema 与容器加载使用同一版本。"""
    code = ("import json; from study_coder.tools.skillload import skill_loader; "
            "loader=skill_loader(); print(json.dumps({'skills': loader.catalog(), "
            "'errors': loader.skill_errors, 'discovery_error': loader.discovery_error}, "
            "ensure_ascii=False))")
    try:
        output = _control(["run", "--rm", "--network", "none", "--read-only",
                           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                           "--user", "10001:10001", image_id,
                           "/opt/study-coder/.venv/bin/python", "-I", "-B", "-c", code], timeout=30)
        payload = json.loads(output)
        if not isinstance(payload, dict) or not isinstance(payload.get("errors"), dict):
            raise ValueError("invalid image skill catalog diagnostics")
        for name, detail in payload["errors"].items():
            skill_loader._log(f"image skipped {name}: {detail}")
        if payload.get("discovery_error"):
            skill_loader._log(str(payload["discovery_error"]))
        return skill_loader.from_catalog(payload.get("skills")).catalog()
    except (SandboxError, ValueError) as exc:
        raise SandboxError("could not read image skill catalog; run study-coder sandbox build") from exc


def build_image(policy: SandboxPolicy) -> None:
    """用项目内固定 Dockerfile 构建策略指定镜像；返回 None。

    Dockerfile 路径由源码位置确定，模型不能提交任意构建文件或构建上下文。
    首次拉取和安装较慢，所以继承终端输出并给单独的构建超时。
    """
    root = Path(__file__).resolve().parents[3]
    dockerfile = root / "sandbox" / "images" / "general.Dockerfile"
    if not dockerfile.is_file():
        raise SandboxError("trusted sandbox Dockerfile is missing")
    command = [_docker_executable(), "build", "--progress", "plain", "-f",
               str(dockerfile), "-t", policy.image, str(root)]
    try:
        # 镜像首次构建可能下载数百 MiB；继承终端输出，避免长时间无进度信息。
        result = subprocess.run(command, timeout=1800, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError("Docker image build failed or timed out") from exc
    if result.returncode:
        raise SandboxError(f"Docker image build failed with exit code {result.returncode}")


class DockerRuntime:
    """一项任务对应一个容器、独立 volume 和宿主任务记录。

    Agent 仅调用 execute_tool；CLI 负责 start/export/apply 等生命周期。
    self.running 是当前对象的状态，record.json 负责跨进程恢复。
    """

    def __init__(self, settings: Settings, policy: SandboxPolicy,
                 record: dict[str, Any]) -> None:
        self.settings = settings
        self.policy = policy
        self.record = record
        self.directory = task_dir(policy.state_dir, record["task_id"])
        self.container = record["container"]
        self.volume = record["volume"]
        self.home_volume = record.get("home_volume", "")
        self.running = record["status"] == "running"
        self.skill_catalog = record.get("skill_catalog", {})
        # self.record 是整个沙箱的生命周期记录；background.jobs 中的 record 才是
        # 各条后台命令的状态。两者的 task_id/job_id、运行状态不能混用。
        # 注入短 RPC 和兜底停止回调，使管理器无需知道 Docker CLI 或容器名称。
        self.background = BackgroundManager(self.directory, policy, self._background_rpc,
                                             self._force_cancel)

    @classmethod
    def start(cls, settings: Settings, policy: SandboxPolicy) -> DockerRuntime:
        """创建任务并返回正在运行的 DockerRuntime。

        顺序是预检 → 导入 tar/快照 → volume → 可选网络/代理 → 任务容器。
        任一步出错都按归属标签回收已建资源，记录失败状态，不退回宿主执行。
        """
        if policy.state_dir == settings.workspace or policy.state_dir.is_relative_to(settings.workspace):
            raise SandboxError("sandbox state directory must be outside the workspace")
        info = doctor(policy)
        skill_catalog = _image_skill_catalog(info["image_id"])
        task_id = uuid.uuid4().hex
        directory = task_dir(policy.state_dir, task_id)
        directory.mkdir(parents=True, exist_ok=False)
        volume = f"study-coder-{task_id}"
        home_volume = f"study-coder-home-{task_id}"
        container = f"study-coder-task-{task_id}"
        task_network = (f"study-coder-net-{task_id}"
                        if policy.service_ports or policy.network == "allowlist" else "")
        proxy_container = f"study-coder-proxy-{task_id}" if policy.network == "allowlist" else ""
        record: dict[str, Any] = {
            "task_id": task_id, "workspace": str(settings.workspace),
            "image": policy.image, "image_id": info["image_id"],
            "volume": volume, "home_volume": home_volume,
            "container": container, "status": "importing",
            "task_network": task_network,
            "proxy_container": proxy_container,
            "policy": {**asdict(policy), "state_dir": str(policy.state_dir)},
            "snapshot": {}, "exported": {}, "diff": {},
            "skill_catalog": skill_catalog,
        }
        save_record(directory, record)
        # 先落盘资源名称与 importing 状态，即使启动中断也能定位待清理任务。
        runtime = cls(settings, policy, record)
        try:
            archive = directory / "import.tar"
            record["snapshot"] = create_import_archive(
                settings.workspace, archive, policy.exclude,
                max_total_bytes=policy.workspace_max_bytes,
                max_files=policy.workspace_max_files)
            save_record(directory, record)
            _control(["volume", "create", "--label", "study-coder.task=" + task_id,
                      volume], timeout=30)
            # HOME 是另一个任务专属卷，不复制宿主 HOME，也不参与项目导出。
            # 镜像中的 /home/agent 归 UID 10001，新卷继承其目录和所有权。
            _control(["volume", "create", "--label", "study-coder.task=" + task_id,
                      home_volume], timeout=30)
            # 镜像中的 /workspace 属于 10001；新 volume 继承该目录权限，
            # 因此初始化也可使用非 root 用户，无需临时增加写权限 capability。
            _control(["run", "--rm", "-i", "--network", "none", "--read-only",
                      "--user", "10001:10001", "--cap-drop", "ALL", "--pids-limit", "128",
                      "--mount", f"type=volume,src={volume},dst=/workspace",
                      info["image_id"], "/bin/sh", "-c",
                      "tar --no-same-owner --no-same-permissions -xf - -C /workspace"],
                     timeout=180, stdin_file=archive)
            archive.unlink(missing_ok=True)
            # 卷已接收受限工作区快照；宿主原目录没有被可写挂载进容器。
            if task_network:
                _control(["network", "create", "--driver", "bridge", "--internal",
                          "--opt", "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                          "--opt", "com.docker.network.bridge.gateway_mode_ipv6=isolated",
                          "--label",
                          "study-coder.task=" + task_id, task_network], timeout=30)
            cache_env = {
                "HOME": "/home/agent", "XDG_CACHE_HOME": "/home/agent/.cache",
                "XDG_CONFIG_HOME": "/home/agent/.config",
                "XDG_DATA_HOME": "/home/agent/.local/share",
                "NPM_CONFIG_CACHE": "/home/agent/.npm",
                "UV_CACHE_DIR": "/home/agent/.cache/uv",
                "GRADLE_USER_HOME": "/home/agent/.gradle",
                "CARGO_HOME": "/home/agent/.cargo", "RUSTUP_HOME": "/home/agent/.rustup",
                "GOPATH": "/home/agent/go", "GOCACHE": "/home/agent/.cache/go-build",
                "NUGET_PACKAGES": "/home/agent/.nuget/packages",
                "STUDY_CODER_EXPORT_EXCLUDE": json.dumps(policy.exclude),
            }
            task_env = [arg for key, value in cache_env.items() for arg in ("--env", key + "=" + value)]
            if proxy_container:
                # 代理有外网桥接接口，任务容器只进 internal 网络；随机令牌仅传给本任务。
                proxy_token = secrets.token_urlsafe(32)
                _control(["run", "-d", "--name", proxy_container, "--label",
                          "study-coder.task=" + task_id, "--network", "bridge",
                          "--read-only", "--cap-drop", "ALL", "--security-opt",
                          "no-new-privileges", "--user", "10001:10001", "--pids-limit", "64",
                          "--cpus", "0.5", "--memory", "256m", "--tmpfs",
                          "/tmp:rw,nosuid,nodev,size=16m", "--env",
                          "STUDY_CODER_PROXY_TOKEN=" + proxy_token, info["image_id"],
                          "python", "-m", "study_coder.sandbox.network_proxy",
                          "--allowlist", json.dumps(policy.network_allowlist), "--max-bytes",
                          str(policy.network_max_bytes),
                          *(["--trust-fake-ip-dns"] if policy.trust_fake_ip_dns else [])],
                         timeout=30)
                # 代理已连接可出站的 bridge。随后接入 internal 网络时显式给它
                # 更低的网关优先级，避免 Docker 改选无外网出口的默认路由。
                _control(["network", "connect", "--gw-priority", "-1",
                          "--alias", "study-coder-proxy",
                          task_network, proxy_container], timeout=20)
                proxy_ready = False
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        _control(["exec", proxy_container, "python", "-c",
                                  "import socket; s=socket.create_connection(('127.0.0.1',3128),1); s.close()"],
                                 timeout=3)
                        proxy_ready = True
                        break
                    except SandboxError:
                        time.sleep(0.25)
                if not proxy_ready:
                    raise SandboxError("sandbox allowlist proxy did not become ready")
                # npm/pip 对只有用户名的代理 URL 处理不一致；显式提供
                # username:password，两个字段均为本任务的随机令牌。
                proxy_url = f"http://{proxy_token}:{proxy_token}@study-coder-proxy:3128"
                for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                             "ALL_PROXY", "all_proxy"):
                    task_env.extend(("--env", name + "=" + proxy_url))
                for name in ("NO_PROXY", "no_proxy"):
                    task_env.extend(("--env", name + "=localhost,127.0.0.1,::1"))
            network_args = (["--network", task_network] if task_network
                            else ["--network", "none"])
            # 只发布预先批准的端口，并只绑定宿主 127.0.0.1。
            port_args = [argument for port in policy.service_ports
                         for argument in ("--publish", f"127.0.0.1:{port}:{port}/tcp")]
            # --init 在 PID 1 处回收退出后的孤儿进程；后台 runner 仍负责清理自己的
            # 命令树。二者共同避免多任务反复启动后积累僵尸进程。
            _control(["run", "-d", "--init", "--name", container, "--label",
                      "study-coder.task=" + task_id, "--user", "10001:10001",
                      "--read-only", "--cap-drop", "ALL", "--security-opt",
                      "no-new-privileges", "--pids-limit", str(policy.pids_limit),
                      "--cpus", str(policy.cpus), "--memory", f"{policy.memory_mb}m",
                      *network_args, *port_args,
                      "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
                      *task_env, "--workdir", "/workspace",
                      "--mount", f"type=volume,src={volume},dst=/workspace",
                      "--mount", f"type=volume,src={home_volume},dst=/home/agent",
                      info["image_id"], "sleep", "infinity"], timeout=60)
            # 长寿命容器只保持 volume 可用；每次工具调用另起短命 Worker。
            runtime.running = True
            record["status"] = "running"
            save_record(directory, record)
            network_note = ("域名代理白名单=" + ",".join(policy.network_allowlist)
                            if proxy_container else
                            ("仅允许回环预览端口 " + ",".join(map(str, policy.service_ports))
                             if task_network else "网络=禁用"))
            print(f"[沙箱] 已启动：profile={policy.profile}，{network_note}，"
                  f"CPU={policy.cpus}，内存={policy.memory_mb}MiB", file=sys.stderr)
            return runtime
        except Exception:
            # cleanup_failed 表示资源可能仍存活，不能把它误报为完全清理。
            cleanup_errors: list[str] = []
            for kind, name in (("container", container), ("container", proxy_container),
                               ("network", task_network), ("volume", volume),
                               ("volume", home_volume)):
                if not name:
                    continue
                try:
                    _remove_owned_resource(kind, name, task_id)
                except SandboxError as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
            record["status"] = "cleanup_failed" if cleanup_errors else "failed"
            if cleanup_errors:
                record["cleanup_errors"] = cleanup_errors
            try:
                save_record(directory, record)
            except OSError:
                pass
            raise
    def _query_tasks(self, function: str) -> Any:
        """通过固定的只读函数查询容器任务；stdout 只承载 JSON。

        固定 /workspace 保证任务模块读取正确的 .tasks。stdout 只承载 JSON；
        stderr 可含诊断信息，因此由退出码判断进程是否成功，再校验 JSON。
        """
        if function not in {"incomplete_tasks", "task_state"}:
            raise SandboxError("invalid internal task query")
        code = (f"import json; from study_coder.tools.task import {function}; "
                f"print(json.dumps({function}(), ensure_ascii=False))")
        try:
            process = subprocess.Popen(
                [_docker_executable(), "exec", "--workdir", "/workspace", self.container,
                 "/opt/study-coder/.venv/bin/python", "-I", "-B", "-c", code],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise SandboxError("sandbox task query could not start") from exc
        try:
            stdout, stderr = process.communicate(timeout=10)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            # 查询只读，无需取消整个容器；终止宿主 docker CLI 并回收管道。
            process.kill()
            process.communicate()
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise SandboxError("sandbox task query timed out after 10s") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:500].strip()
            raise SandboxError(
                f"sandbox task query failed (exit {process.returncode}): "
                f"{detail or 'no stderr output'}")
        try:
            return json.loads(stdout)
        except (ValueError, UnicodeError) as exc:
            raise SandboxError("sandbox task query returned invalid JSON") from exc

    def get_task_state(self) -> str:
        """每轮重新查询，确保模型看到上轮工具刚刚更新的任务状态。"""
        state = self._query_tasks("task_state")
        if not isinstance(state, str) or len(state) > TASK_STATE_MAX_CHARS:
            raise SandboxError("sandbox task query returned invalid context state")
        return state

    def get_incomplete_tasks(self) -> dict[str, str]:
        """空字典表示无未完成任务；查询失败必须抛出异常。"""
        tasks = self._query_tasks("incomplete_tasks")
        if not isinstance(tasks, dict):
            raise SandboxError("sandbox task query must return an object")
        for task_id, status in tasks.items():
            if (not isinstance(task_id, str)
                    or not re.fullmatch(r"task_[0-9a-f]{8}", task_id)
                    or not isinstance(status, str)
                    or status not in {"pending", "in_progress"}):
                raise SandboxError("sandbox task query returned invalid task state")
        return tasks

    def _bounded_exec(self, request: bytes, timeout_s: int) -> tuple[bytes, bytes]:
        """运行一次短命 Worker，返回协议 stdout/stderr 的原始字节。

        输入是受限 JSON 请求与本次超时；两路输出和 stdin 都由独立线程处理，
        防止管道互相等待。Worker 超时之外再留 5 秒作为宿主 watchdog；
        若 Worker/ Docker exec 卡住，则取消整个任务容器以停止残留写入者。
        """
        process = subprocess.Popen(
            [_docker_executable(), "exec", "-i", self.container, "/opt/study-coder/.venv/bin/python", "-I","-B","-m",
             "study_coder.sandbox.worker"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        overflow = threading.Event()

        def drain(name: str, pipe: Any) -> None:
            # JSON 可能把一个控制字节转义为多个字符，协议预算需计入编码开销。
            limit = self.policy.command_output_bytes * 6 + 65_536
            while chunk := pipe.read(65_536):
                remaining = limit - len(output[name])
                if remaining > 0:
                    output[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
            pipe.close()

        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        input_pipe = process.stdin
        writer_errors: list[OSError] = []

        def send_request() -> None:
            # 请求可能遇到 Worker 提前退出；写入异常由主线程统一处理。
            try:
                input_pipe.write(request)
                input_pipe.flush()
            except OSError as exc:
                writer_errors.append(exc)
            finally:
                try:
                    input_pipe.close()
                except OSError as exc:
                    writer_errors.append(exc)

        readers = [threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
                   threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True)]
        for reader in readers:
            reader.start()
        started = time.monotonic()
        writer = threading.Thread(target=send_request, daemon=True)
        writer.start()
        try:
            next_progress = 30
            while process.poll() is None:
                if overflow.is_set():
                    raise SandboxError("sandbox protocol output limit exceeded")
                elapsed = time.monotonic() - started
                if elapsed >= next_progress:
                    print(f"[命令运行] 已运行 {next_progress} 秒，仍在等待退出",
                          file=sys.stderr, flush=True)
                    next_progress += 30
                if elapsed > timeout_s + 5:
                    raise SandboxError(f"sandbox tool timed out after {timeout_s}s")
                time.sleep(0.1)
            writer.join(timeout=2)
            if writer.is_alive():
                raise SandboxError("sandbox request write did not complete")
            if writer_errors:
                raise SandboxError(f"sandbox request could not be sent: {writer_errors[0]}")
        except (KeyboardInterrupt, SandboxError, OSError) as failure:
            # 取消容器比只杀宿主 docker CLI 更重要：命令仍可能在容器内运行。
            cleanup_failure: Exception | None = None
            try:
                self.cancel()
            except Exception as exc:
                cleanup_failure = exc
            finally:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                process.wait()
            if cleanup_failure is not None:
                raise SandboxError(f"{failure}; container cancellation failed: "
                                   f"{cleanup_failure}") from failure
            raise
        finally:
            writer.join(timeout=1)
            for reader in readers:
                reader.join(timeout=2)
        if overflow.is_set():
            self.cancel()
            raise SandboxError("sandbox protocol output limit exceeded")
        if process.returncode:
            detail = output["stderr"].decode("utf-8", errors="replace")[:500].strip()
            raise SandboxError(
                f"sandbox worker failed (exit {process.returncode}): "
                f"{detail or 'no stderr output'}")
        return bytes(output["stdout"]), bytes(output["stderr"])

    def _background_rpc(self, request: dict[str, Any]) -> dict[str, Any]:
        """发送内部控制字典，解析并返回容器控制入口的一份 JSON 响应。

        这是一次短 docker exec，不是后台命令进程本身。10 秒只限制控制请求，
        命令执行时间由 runner 的 timeout_s 控制；调用失败只回收本次宿主 CLI。
        持续异常是否停止整个容器，由 BackgroundManager 的生命周期逻辑决定。
        """
        if not self.running:
            raise SandboxError("sandbox container is stopped")
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")  # 发给 stdin 的请求字节。
        if len(payload) > 65536:
            raise SandboxError("background request exceeds 64 KiB")
        # process 是 Windows 宿主的 docker CLI 进程，不是容器内 runner 或 shell。
        # 使用镜像内固定解释器，-I 隔离工作区同名包，避免执行模型写入的入口代码。
        process = subprocess.Popen(
            [_docker_executable(), "exec", "-i", "--workdir", "/workspace", self.container,
             "/opt/study-coder/.venv/bin/python", "-I", "-B", "-m",
             "study_coder.sandbox.background_runner", "--control"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outputs = {"stdout": bytearray(), "stderr": bytearray()}  # 机器协议输出与控制诊断分别保留。
        overflow = threading.Event()  # stdout 超限标记；剪断协议后不能继续按完整 JSON 使用。
        errors: list[OSError] = []  # 三条 I/O 线程的管道失败，交回主线程统一报告。

        def drain(name: str, pipe: Any, limit: int) -> None:
            """排空一条 CLI 输出管道；limit 为最多保留的字节数，满额后仍继续读。"""
            try:
                while chunk := pipe.read(65536):
                    remaining = limit - len(outputs[name])  # 当前缓冲还能保留的字节数。
                    outputs[name].extend(chunk[:max(0, remaining)])
                    if name == "stdout" and len(chunk) > remaining:
                        overflow.set()
            except OSError as exc:
                errors.append(exc)
            finally:
                pipe.close()

        def send() -> None:
            """写入一份请求后关闭 stdin；控制入口依赖 EOF 完成有界读取。"""
            assert process.stdin is not None
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except OSError as exc:
                errors.append(exc)
            finally:
                process.stdin.close()

        assert process.stdout is not None and process.stderr is not None
        # 同时读两个流并写请求，避免一边管道写满、另一边等待造成相互阻塞。
        # stdout 容量覆盖最大 8 MiB 日志的 base64 膨胀和状态 JSON；stderr 仅留诊断。
        readers = [threading.Thread(target=drain, args=("stdout", process.stdout, 12 * 1024**2), daemon=True),
                   threading.Thread(target=drain, args=("stderr", process.stderr, 16384), daemon=True)]
        writer = threading.Thread(target=send, daemon=True)
        for thread in (*readers, writer):
            thread.start()
        deadline = time.monotonic() + 10  # 单次控制传输截止点，与模型选择的命令秒数无关。
        try:
            while process.poll() is None:
                if overflow.is_set():
                    raise SandboxError("background control output limit exceeded")
                if time.monotonic() >= deadline:
                    raise SandboxError("background control timed out after 10s")
                time.sleep(0.05)
        finally:
            # 本次查询的 CLI 必须退出并回收。停止 CLI 不等于确认命令没启动，因此
            # 启动响应丢失时管理器保留原编号继续查询，不能盲目重发一条新命令。
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for thread in (*readers, writer):
                thread.join(timeout=2)
        if overflow.is_set() or errors or any(t.is_alive() for t in (*readers, writer)):
            raise SandboxError("background control output or pipe failed")
        if process.returncode:
            detail = outputs["stderr"].decode("utf-8", errors="replace")[:500]
            raise SandboxError(f"background control failed: {detail}")
        try:
            response = json.loads(bytes(outputs["stdout"]))
        except ValueError as exc:
            raise SandboxError("invalid background control JSON") from exc
        if not isinstance(response, dict):
            raise SandboxError("invalid background control object")
        return response

    def get_background_state(self) -> str:
        """返回模型请求用的有限元数据；禁用功能时不添加后台状态前缀。"""
        return self.background.state() if self.policy.background_enabled else ""

    def get_background_pending(self) -> tuple[str, ...]:
        """最终答复前刷新状态，返回仍运行／未交付／未知等未完成原因。"""
        # 完成前主动刷新，关闭生成答复期间任务恰好完成的竞态窗口。
        if self.running and self.background.jobs:
            self.background.refresh()
        return self.background.pending()

    def acknowledge_background(self, job_ids: list[str]) -> None:
        """job_ids 是已保存完整历史的终态执行编号，不是工具 call_id。"""
        self.background.acknowledge(job_ids)

    def background_job_ids(self) -> set[str]:
        """复制本沙箱已登记的执行编号，用于区分本轮新建任务与之前已有任务。"""
        with self.background._lock:
            return set(self.background.jobs)

    def interrupt_background(self, job_ids: set[str]) -> None:
        """只取消调用方指定的任务集合；正常单任务取消不停止整个容器。"""
        for identifier in job_ids:
            self.background.cancel(identifier)

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """后台工具交管理器；其他允许的工具封成 JSON 交 Worker，返回 ToolResult。

        模型可选 bash timeout_s，但先受类型/范围检查，再受宿主 policy 上限限制。
        Docker/Worker/协议异常转换为工具错误，供 Agent 当作观察继续处理。
        """
        if name not in SANDBOX_TOOLS:
            return ToolResult("", name, "Error: tool is not a sandbox workspace tool", True)
        if not self.running:
            return ToolResult("", name, "Error: sandbox container is stopped", True)
        if name in BACKGROUND_TOOLS:
            # 后台等待可能长于普通 Worker 请求；提前走独立控制路径，避免共用
            # _bounded_exec 的整容器取消兜底，也无需普通 Worker 持有后台进程。
            if not self.policy.background_enabled:
                return ToolResult("", name, "Error: background tasks are disabled", True)
            return self.background.execute(name, arguments, self.settings.max_tool_output_chars)
        supplied = arguments.get("timeout_s", self.settings.command_timeout_s)
        if name == "bash" and (type(supplied) is not int or not 1 <= supplied <= 86_400):
            return ToolResult("", name, "Error: timeout_s must be 1..86400 seconds", True)
        timeout_s = supplied if name == "bash" else self.settings.command_timeout_s
        if timeout_s > self.policy.max_command_timeout_s:
            return ToolResult("", name, "Error: timeout_s exceeds sandbox profile limit", True)
        request = json.dumps({"name": name, "arguments": arguments,
                              "timeout_s": timeout_s,
                              "output_bytes": self.policy.command_output_bytes,
                              "workspace_max_bytes": self.policy.workspace_max_bytes,
                              "workspace_max_files": self.policy.workspace_max_files,
                              "service_ports": list(self.policy.service_ports),
                              "max_tool_output_chars": self.settings.max_tool_output_chars,
                              "max_skill_chars": self.settings.max_skill_chars},
                             ensure_ascii=False).encode("utf-8")
        if len(request) > 1_048_576:
            return ToolResult("", name, "Error: tool request exceeds 1 MiB", True)
        try:
            stdout, _ = self._bounded_exec(request, timeout_s)
            # stdout 是机器协议，只接受结构和工具名都匹配的一份 JSON 结果。
            response = json.loads(stdout)
            if not isinstance(response, dict) or response.get("name") != name:
                raise SandboxError("sandbox worker returned an invalid response")
            content = response.get("content")
            error = response.get("is_error")
            if not isinstance(content, str) or not isinstance(error, bool):
                raise SandboxError("sandbox worker returned an invalid result")
            return ToolResult("", name, content, error)
        except (SandboxError, OSError, ValueError) as exc:
            return ToolResult("", name, f"Error: {exc}", True)

    def cancel(self) -> None:
        """强制停止任务容器并确认状态；用于工具调用失控时终止所有写入者。"""
        try:
            # 正常路径先让管理器逐项停止并收集日志；强制取消最终仍确保容器停止。
            self.background.shutdown()
        finally:
            self._force_cancel()

    def _force_cancel(self) -> None:
        """控制失效的兜底；不调用 shutdown，避免监测线程递归等待自己。"""
        if self.running:
            try:
                _control(["kill", self.container], timeout=15)
                self._ensure_stopped()
            except SandboxError:
                self.record["status"] = "cleanup_failed"
                raise
            else:
                self.record["status"] = "stopped"
            finally:
                self.running = False
                save_record(self.directory, self.record)
            self.background.mark_container_stopped()

    def _ensure_stopped(self) -> None:
        """Docker stop/kill 返回后，再读取真实容器状态。"""
        state = _control(["inspect", "--format", "{{.State.Running}}", self.container],
                         timeout=15)
        if state != "false":
            raise SandboxError("sandbox container is still running")

    def get_export_issues(self, exclusions: tuple[str, ...] | None = None) -> list[str]:
        """在容器中执行宿主可信预检，返回有界列表，不停止任务或修改文件。"""
        if not self.running:
            raise SandboxError("sandbox container is stopped")
        selected = exclusions if exclusions is not None else (
            *self.policy.exclude, *self.record.get("export_exclude", []))
        options = json.dumps({"extra_exclude": selected,
                              "max_total_bytes": self.policy.workspace_max_bytes,
                              "max_files": self.policy.workspace_max_files})
        # Python 脚本来自安装位置的 transfer.py，模型只能影响 JSON 中的项目
        # 文件，不能指定脚本或宿主路径。使用 stdin 也兼容仍在旧镜像中的任务。
        raw = _control(["exec", "-i", self.container, "/opt/study-coder/.venv/bin/python",
                        "-I", "-B", "-", options], timeout=30,
                       stdin_file=Path(__file__).with_name("transfer.py"))
        try:
            issues = json.loads(raw)
        except ValueError as exc:
            raise SandboxError("invalid export preflight response; rebuild the sandbox image") from exc
        if (not isinstance(issues, list) or len(issues) > 21
                or any(not isinstance(item, str) or len(item) > 2000 for item in issues)):
            raise SandboxError("invalid export preflight response")
        return issues

    def export_changes(self, *, extra_exclude: tuple[str, ...] = ()) -> dict[str, list[str]]:
        """停止任务容器，从只读 volume 导出到 staging，返回差异表。

        成功时记录导出哈希和 awaiting_apply，然后回收容器/网络/volume；
        导出失败时记录 export_failed，保留 volume 供重试。
        显式新增排除规则单独保存，重试时继续使用；任务创建时的策略与快照保留。
        """
        previous = self.record.get("export_exclude", [])
        if not isinstance(previous, list) or any(not isinstance(p, str) for p in previous):
            raise SandboxError("sandbox task has invalid saved export exclusions")
        requested = tuple(p.replace("\\", "/").removeprefix("./").rstrip("/")
                          for p in extra_exclude)
        if any(not p or "\0" in p or p.startswith("/") or ":" in p
               or any(part in {".", ".."} for part in p.split("/"))
               for p in requested):
            raise SandboxError("export exclusions must be nonempty workspace-relative patterns")
        additions = tuple(p for p in requested if p not in (*self.policy.exclude, *previous))
        if self.record["status"] in {"awaiting_apply", "applied"}:
            if additions:
                raise SandboxError("sandbox task is already exported; cannot add export exclusions")
            return self.record["diff"]
        if self.record["status"] == "cleanup_failed":
            raise SandboxError("sandbox cleanup failed; verify container state before export")
        if additions:
            self.record["export_exclude"] = list(dict.fromkeys((*previous, *additions)))
            # 先持久化显式选择，控制、归档或 tar 失败后再次 export 可继续使用。
            save_record(self.directory, self.record)
        exclusions = (*self.policy.exclude, *self.record.get("export_exclude", []))
        if self.running:
            issues = self.get_export_issues(exclusions)
            if issues:
                # 在 stop/shutdown 前返回，保留模型可修复的运行环境与唯一文件来源。
                # --exclude 仍须用户明确指定，不静默丢弃任何不符合交付规则的产物。
                raise SandboxError("export preflight failed; sandbox task " + self.record["task_id"]
                                   + " remains running. Fix these files or retry sandbox export with "
                                   "--exclude PATTERN:\n" + "\n".join(issues))
        if not self.running:
            self._ensure_stopped()
            # 容器先前已停，未归档任务不能凭旧状态推断成功；显式记录 lost。
            self.background.mark_container_stopped()
        # /tmp 中的控制日志不在工作区 volume 内，必须在 stop 前取回；归档的
        # 普通磁盘错误在这里抛出，以保留唯一容器日志来源供用户重试。
        self.background.shutdown()
        if self.running:
            _control(["stop", "--time", "3", self.container], timeout=15)
            self.running = False
        self._ensure_stopped()
        # 先确认写入者已停止，才允许把 volume 当成稳定快照导出。
        self.record["status"] = "exporting"
        self.record.pop("export_error", None)
        save_record(self.directory, self.record)
        export_image = self.record["image_id"]
        try:
            try:
                _control(["image", "inspect", export_image, "--format", "{{.Id}}"], timeout=15)
            except SandboxError as exc:
                if "no such image" not in str(exc).casefold():
                    raise
                # Docker Desktop 可能在重建同名标签时回收旧 manifest。导出器只需
                # 可信镜像里的 tar，且只读挂载旧 volume；重新预检当前策略镜像后继续。
                export_image = doctor(self.policy)["image_id"]
        except (SandboxError, OSError):
            self.record["status"] = "export_failed"
            save_record(self.directory, self.record)
            raise
        exporter = f"study-coder-export-{self.record['task_id']}"
        command = [_docker_executable(), "run", "--rm", "--name", exporter,
                   "--label", "study-coder.task=" + self.record["task_id"],
                   "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--user", "10001:10001",
                   "--mount", f"type=volume,src={self.volume},dst=/workspace,readonly",
                   export_image, "tar", "--hard-dereference", "-C", "/workspace", "-cf", "-",
                   "--no-wildcards-match-slash",
                   *[f"--exclude={name}" for name in tar_exclude_patterns(exclusions)], "."]
        # 导出容器只读挂载同一 volume，tar 写到 stdout，宿主再逐成员校验。
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None and process.stderr is not None
        stderr_pipe = process.stderr
        staging_name = "export-" + uuid.uuid4().hex
        staging = self.directory / staging_name
        stderr_bytes = bytearray()
        timed_out = threading.Event()

        def read_stderr() -> None:
            while chunk := stderr_pipe.read(8192):
                remaining = 8192 - len(stderr_bytes)
                if remaining > 0:
                    stderr_bytes.extend(chunk[:remaining])

        def stop_exporter() -> None:
            timed_out.set()
            try:
                _remove_owned_resource("container", exporter, self.record["task_id"])
            except SandboxError:
                pass
            if process.poll() is None:
                process.kill()

        stderr_reader = threading.Thread(target=read_stderr, daemon=True)
        stderr_reader.start()
        watchdog = threading.Timer(180, stop_exporter)
        watchdog.start()
        try:
            exported = extract_export(
                cast(BinaryIO, process.stdout), staging, exclusions,
                max_total_bytes=self.policy.workspace_max_bytes,
                max_files=self.policy.workspace_max_files)
            # tarfile 的 r|* 模式并不按需读满管道：它先向底层取 BLOCKSIZE=512
            # 字节识别压缩格式，此后每次固定请求 RECORDSIZE=10240 字节，读取
            # 位置因此落在 512 + k*10240 的网格上。GNU tar 又把归档补齐到 10240
            # 的整数倍，于是归档末尾最多 9728 字节永远落在网格之外。tarfile 见到
            # 归档结束标记就停手，剩下的字节留在管道里，导出容器阻塞在写 stdout
            # 上永不退出——wait() 给多少秒都只会超时。这里显式读干净，导出容器
            # 才能正常收尾；多出来的字节只是 tar 的补零填充，已经校验过的内容不受影响。
            process.stdout.read()
            process.wait(timeout=30)
            if timed_out.is_set():
                raise SandboxError("Docker export timed out")
            if process.returncode:
                detail = stderr_bytes.decode("utf-8", errors="replace")[:500].strip()
                raise SandboxError(
                    f"Docker export failed (exit {process.returncode}): "
                    f"{detail or 'no stderr output'}")
        except Exception as exc:
            if process.poll() is None:
                process.kill()
            process.wait()
            try:
                _remove_owned_resource("container", exporter, self.record["task_id"])
            except SandboxError:
                pass
            stderr_reader.join(timeout=2)
            failure = _format_export_failure(exc, process.returncode,
                                             bytes(stderr_bytes))
            # 失败目录只含未发布的部分结果；重试前清除，避免 staging 持续累积。
            shutil.rmtree(staging, ignore_errors=True)
            self.record["status"] = "export_failed"
            self.record["export_error"] = str(failure)[:2_000]
            save_record(self.directory, self.record)
            if failure is exc:
                raise
            raise failure from exc
        finally:
            watchdog.cancel()
            stderr_reader.join(timeout=2)
        self.record["exported"] = exported
        # 只有 tar 全部通过校验，才发布可供 review/apply 的导出目录与哈希。
        self.record["export_dir"] = staging_name
        # 应用使用同一过滤基线；被排除的宿主原文件不会进入 deleted 或被回写。
        self.record["apply_snapshot"] = filter_snapshot(self.record["snapshot"], exclusions)
        diff = changes(self.record["apply_snapshot"], exported)
        self.record["diff"] = diff
        self.record["status"] = "awaiting_apply"
        save_record(self.directory, self.record)
        try:
            _remove_owned_resource("container", self.container, self.record["task_id"])
            proxy_container = self.record.get("proxy_container")
            if isinstance(proxy_container, str) and proxy_container:
                _remove_owned_resource("container", proxy_container, self.record["task_id"])
            task_network = self.record.get("task_network")
            if isinstance(task_network, str) and task_network:
                _remove_owned_resource("network", task_network, self.record["task_id"])
            _remove_owned_resource("volume", self.volume, self.record["task_id"])
            if self.home_volume:
                _remove_owned_resource("volume", self.home_volume, self.record["task_id"])
        except SandboxError as exc:
            self.record["cleanup_error"] = str(exc)
            save_record(self.directory, self.record)
        return diff

    def close(self) -> None:
        """任务还在运行时导出成果；已结束/导出的任务不重复处理。"""
        # close、export、cancel 共用幂等 shutdown，防止不同退出入口漏收尾。
        self.background.shutdown()
        if self.record["status"] == "running":
            self.export_changes()


def policy_for_task(current: SandboxPolicy, record: dict[str, Any]) -> SandboxPolicy:
    """从任务记录恢复启动时的策略，并核对当前状态目录。

    导出重试可能发生在 CLI 重启后；仍用任务创建时的排除规则和预算，
    避免当前 .env 改动悄悄改变同一任务的导出边界。
    """
    stored = record.get("policy")
    if not isinstance(stored, dict):
        raise SandboxError("sandbox task has no saved policy")
    try:
        values = dict(stored)
        values["state_dir"] = Path(values["state_dir"])
        values["exclude"] = tuple(values.get("exclude", ()))
        values["network_allowlist"] = tuple(values.get("network_allowlist", ()))
        values["service_ports"] = tuple(values.get("service_ports", ()))
        saved = SandboxPolicy(**values)
    except (KeyError, TypeError, ValueError) as exc:
        raise SandboxError("sandbox task policy record is invalid") from exc
    if saved.state_dir != current.state_dir:
        raise SandboxError("sandbox task state directory does not match current configuration")
    return saved


def apply_task(policy: SandboxPolicy, task_id: str) -> dict[str, list[str]]:
    """将已审查导出应用到原工作区；返回 added/modified/deleted。

    所需快照与 staging 均保存在宿主任务目录，因此应用时无需 Docker。
    遇上次中断的 applying 状态，先尝试恢复回滚，再重新应用。
    """
    record = load_record(policy.state_dir, task_id)
    if record.get("status") == "applying":
        recover_task(policy, task_id)
        record = load_record(policy.state_dir, task_id)
    if record.get("status") != "awaiting_apply":
        raise SandboxError("sandbox task has no reviewed export to apply")
    directory = task_dir(policy.state_dir, task_id)
    stage = _task_artifact(directory, record.get("export_dir"), _EXPORT_DIR_NAME, "export")
    journal_name = "apply-" + uuid.uuid4().hex + ".json"
    record["apply_journal"] = journal_name
    record["status"] = "applying"
    save_record(directory, record)
    try:
        result = apply_changes(Path(record["workspace"]), stage,
                               record.get("apply_snapshot", record["snapshot"]),
                               record["exported"], directory / journal_name)
    except SandboxError:
        journal = directory / journal_name
        try:
            journal_status = json.loads(journal.read_text(encoding="utf-8")).get("status")
        except FileNotFoundError:
            journal_status = "not_started"
        except (OSError, ValueError):
            journal_status = "recovery_required"
        record["status"] = ("awaiting_apply" if journal_status in {"rolled_back", "not_started"}
                            else "apply_recovery_required")
        save_record(directory, record)
        raise
    record["status"] = "applied"
    save_record(directory, record)
    return result


def recover_task(policy: SandboxPolicy, task_id: str) -> None:
    """按日志恢复中断的应用事务；成功后任务回到 awaiting_apply。

    如果尚未写入日志，只清理准备目录；若已有日志，则由 recover_apply
    检查每个文件当前哈希，遇第三方改动保留现场并报告冲突。
    """
    record = load_record(policy.state_dir, task_id)
    if record.get("status") not in {"applying", "apply_recovery_required"}:
        raise SandboxError("sandbox task has no interrupted apply to recover")
    journal_name = record.get("apply_journal")
    directory = task_dir(policy.state_dir, task_id)
    journal_path = _task_artifact(directory, journal_name, _APPLY_JOURNAL_NAME,
                                  "apply recovery journal")
    if not journal_path.exists():
        transaction_dir = _task_artifact(directory, journal_path.stem,
                                         _APPLY_DIR_NAME, "apply transaction")
        if transaction_dir.is_dir():
            import shutil
            shutil.rmtree(transaction_dir)
        record["status"] = "awaiting_apply"
        save_record(directory, record)
        return
    recover_apply(Path(record["workspace"]), journal_path)
    record["status"] = "awaiting_apply"
    save_record(directory, record)


def cleanup_task(policy: SandboxPolicy, task_id: str, *, discard: bool = False) -> str:
    """清理本任务的 Docker 资源并返回最终状态。

    默认先尽力导出仍在运行或已停止的任务；discard 才允许丢弃未导出卷。
    删除前逐一检查资源标签，避免误删同名但不归本任务的资源。
    """
    record = load_record(policy.state_dir, task_id)
    directory = task_dir(policy.state_dir, task_id)
    if record.get("status") in {"applying", "apply_recovery_required"}:
        recover_task(policy, task_id)
        record = load_record(policy.state_dir, task_id)
    if record.get("status") in {"running", "stopped"} and not discard:
        settings = Settings("sandbox", None, None, Path(record["workspace"]))
        runtime = DockerRuntime(settings, policy_for_task(policy, record), record)
        runtime.export_changes()
        record = load_record(policy.state_dir, task_id)
    status = record.get("status")
    if status in {"importing", "exporting", "export_failed", "applying",
                  "apply_recovery_required"} and not discard:
        raise SandboxError("task has unexported or interrupted changes; recover/export first, "
                           "or pass --discard to delete its volume")
    if status == "awaiting_apply" and not discard:
        # Verified changes are already stored in the task directory.
        pass
    for kind, name_key in (("container", "container"), ("container", "proxy_container"),
                           ("network", "task_network"), ("volume", "volume"),
                           ("volume", "home_volume")):
        name = record.get(name_key)
        if isinstance(name, str) and name:
            _remove_owned_resource(kind, name, task_id)
    record["resources_removed"] = True
    if discard:
        record["status"] = "discarded"
        (directory / "import.tar").unlink(missing_ok=True)
        stage_name = record.get("export_dir")
        if isinstance(stage_name, str) and _EXPORT_DIR_NAME.fullmatch(stage_name):
            import shutil
            stage = _task_artifact(directory, stage_name, _EXPORT_DIR_NAME, "export")
            if stage.is_dir():
                shutil.rmtree(stage, ignore_errors=True)
        for journal in directory.glob("apply-*.json"):
            if not _APPLY_JOURNAL_NAME.fullmatch(journal.name):
                continue
            journal = _task_artifact(directory, journal.name, _APPLY_JOURNAL_NAME,
                                     "apply recovery journal")
            journal.unlink(missing_ok=True)
            transaction_dir = _task_artifact(directory, journal.stem,
                                             _APPLY_DIR_NAME, "apply transaction")
            if transaction_dir.is_dir():
                import shutil
                shutil.rmtree(transaction_dir, ignore_errors=True)
    elif status == "applied":
        record["status"] = "cleaned"
    save_record(directory, record)
    return str(record.get("status", "cleaned"))


def review_task(policy: SandboxPolicy, task_id: str) -> str:
    """返回文本 diff；大文件与非 UTF-8 文件只列路径，不改变任何文件。"""
    record = load_record(policy.state_dir, task_id)
    if record.get("status") not in {"awaiting_apply", "applied"}:
        raise SandboxError("sandbox task has no verified export to review")
    directory = task_dir(policy.state_dir, task_id)
    root = Path(record["workspace"])
    stage = _task_artifact(directory, record.get("export_dir"), _EXPORT_DIR_NAME, "export")
    lines: list[str] = []
    for kind, paths in record["diff"].items():
        for relative in paths:
            lines.append(f"{kind}: {relative}")
            if kind == "deleted":
                continue
            source = stage / relative
            original = root / relative
            if source.stat().st_size > 65_536 or (original.is_file() and
                                                  original.stat().st_size > 65_536):
                lines.append("  [large file; inspect staged copy]")
                continue
            try:
                new = source.read_text(encoding="utf-8").splitlines(keepends=True)
                old = (original.read_text(encoding="utf-8").splitlines(keepends=True)
                       if original.is_file() else [])
            except UnicodeError:
                lines.append("  [binary file; inspect staged copy]")
                continue
            lines.extend(difflib.unified_diff(old, new, fromfile=f"a/{relative}",
                                              tofile=f"b/{relative}"))
            if sum(len(item) for item in lines) > 100_000:
                lines.append("[review output truncated; inspect staged files]")
                return "\n".join(lines)[:100_000]
    return "\n".join(lines) if lines else "No file changes"
