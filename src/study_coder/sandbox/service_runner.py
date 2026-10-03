"""受监控的常驻预览命令；独立进程组由 Worker 的 stop_service 管理。"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _workspace_usage(root: Path, max_bytes: int, max_files: int) -> tuple[int, int]:
    """返回 (扫描累计大小, 成员数量)，超 max_bytes/max_files 时提前返回。

    服务和后台 runner 共用；按 lstat 累加条目大小，目录也计入成员数量。
    这是周期性近似检查，不是文件系统硬配额，也不是某个任务独占的额度。
    """
    total = files = 0  # total 按字节计；files 实际包含目录和文件等条目。
    for base, dirs, names in os.walk(root, followlinks=False):
        current = Path(base)
        for name in (*dirs, *names):
            try:
                info = (current / name).lstat()
                files += 1
                total += info.st_size
                if total > max_bytes or files > max_files:
                    return total, files
            except OSError:
                continue
    return total, files


def _runner_descendants() -> set[int]:
    """仅扫描当前 runner 的后代；兄弟 runner 和 Worker 不在此集合中。"""
    parent: dict[int, int] = {}  # /proc 当次快照：子进程 PID -> 父进程 PPID。
    with os.scandir("/proc") as entries:
        for entry in entries:
            if entry.name.isdecimal():
                try:
                    raw = Path(entry.path, "stat").read_text(encoding="ascii")
                    fields = raw[raw.rfind(")") + 2:].split()
                    parent[int(entry.name)] = int(fields[1])
                except FileNotFoundError:
                    continue
    descendants: set[int] = set()  # 已发现的当前 runner 的后代 PID，不含 runner 自己。
    frontier = [os.getpid()]  # 待向下展开的父 PID；从当前 runner 开始遍历进程树。
    while frontier:
        current = frontier.pop()
        found = [pid for pid, ppid in parent.items()  # current 的直接子进程，后续继续找孙进程。
                 if ppid == current and pid not in descendants]
        descendants.update(found)
        frontier.extend(found)
    return descendants


def _kill_descendants() -> bool:
    """结束、回收后代，返回是否确认全部清理；后台任务据此提交终态。"""
    for _ in range(5):
        # 进程退出／被 subreaper 收养会改变父子关系，有限次重扫而不是只用旧快照。
        try:
            descendants = _runner_descendants()
        except (OSError, ValueError, IndexError):
            return False
        if not descendants:
            return True
        for pid in descendants:
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except ProcessLookupError:
                pass
        while True:
            try:
                # -1 选择任意子进程；WNOHANG 只回收已经退出项，避免等待活进程。
                # waited>0 表示本次回收的 PID；0 表示此刻没有可回收的已退出子进程。
                waited, _ = os.waitpid(-1, getattr(os, "WNOHANG", 1))
            except ChildProcessError:
                break
            if waited == 0:
                break
        time.sleep(0.02)
    try:
        return not _runner_descendants()
    except (OSError, ValueError, IndexError):
        return False


def main() -> int:
    """启动前台服务，周期监控配额，并在停止/退出时收拾其子进程。

    start_service 通过独立进程组启动本模块；当前 Worker 退出后它仍存活。
    stop_service 对该组发终止信号，本函数接收信号后结束服务命令。
    """
    try:
        config: dict[str, Any] = json.loads(sys.argv[1])
        workspace = Path(config["workspace"])
        max_bytes = int(config["workspace_max_bytes"])
        max_files = int(config["workspace_max_files"])
        command = str(config["command"])
    except (IndexError, KeyError, TypeError, ValueError):
        return 2
    try:
        ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)
    except (AttributeError, OSError):
        pass
    stop_requested = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    # 与普通 bash 不同，服务应持续前台运行；stdout/stderr 不回灌一次工具结果。
    process = subprocess.Popen(["/bin/sh", "-c", command], cwd=workspace,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=False,
                               close_fds=True)
    exit_code = 0
    while process.poll() is None and not stop_requested:
        # 逻辑扫描并非磁盘硬配额；超限时停止受托管的服务进程。
        size, count = _workspace_usage(workspace, max_bytes, max_files)
        if size > max_bytes or count > max_files:
            exit_code = 3
            break
        time.sleep(0.5)
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    _kill_descendants()
    return exit_code or int(process.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
