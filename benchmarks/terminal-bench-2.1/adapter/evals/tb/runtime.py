"""Harbor 环境上的 WorkspaceRuntime 垫片。

Agent 只认识 ``execute_tool(name, arguments)``；本模块把 DockerRuntime 的
``docker exec worker.py`` 换成 Harbor 的 ``environment.exec``。请求体字段、
1 MiB 上限和响应校验都与 ``sandbox/docker.py`` 保持一致，因此容器内的工具
语义不变，``src/`` 不需要改动。

同步/异步桥：``Agent.run`` 是阻塞循环，而 Harbor 的 ``BaseAgent.run`` 在事件
循环里。宿主线程通过 ``run_coroutine_threadsafe`` 把每次工具调用交回该循环。

请求体不塞进命令行：Windows 宿主上 ``docker compose exec`` 的 argv 受
CreateProcess 32767 字符限制，1 MiB 请求一定放不下。因此每次调用先用
``environment.upload_file`` 写 ``/tmp/coder/request.json``，容器内结果
写到文件再用哨兵标记回传，避免 docker compose 自身的告警污染 JSON。
"""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
import threading
from pathlib import Path
from typing import Any, Awaitable, TypeVar

from coder.config import Settings
from coder.sandbox.base import SANDBOX_TOOLS, SandboxError
from coder.sandbox.policy import SandboxPolicy
from coder.tools.background import BACKGROUND_TOOLS
from coder.types import ToolResult

_T = TypeVar("_T")

WORKER_DIR = "/tmp/coder"
WORKER_PATH = f"{WORKER_DIR}/worker.pyz"
REQUEST_PATH = f"{WORKER_DIR}/request.json"
RESPONSE_PATH = f"{WORKER_DIR}/response.json"
ERROR_PATH = f"{WORKER_DIR}/response.err"
RESULT_BEGIN = "__CODER_RESULT_BEGIN__"
RESULT_END = "__CODER_RESULT_END__"
ERROR_BEGIN = "__CODER_ERROR_BEGIN__"
ERROR_END = "__CODER_ERROR_END__"
# Worker 自己按 timeout_s 收尾；这里再给 docker exec 的启动与回传留出余量。
_EXEC_MARGIN_S = 15
_EXEC_GRACE_S = 30
# 工作区不导出，交付预检的排除规则在这里只会造成误报（符号链接、超大文件）。
_EXPORT_EXCLUDE = '["*"]'


def _between(text: str, begin: str, end: str) -> str | None:
    """取哨兵之间的原文；缺失任一哨兵时返回 None。"""
    start = text.find(begin)
    if start < 0:
        return None
    start += len(begin)
    stop = text.find(end, start)
    if stop < 0:
        return None
    return text[start:stop]


class HarborRuntime:
    """把 Harbor 的 ``environment.exec`` 适配成 ``WorkspaceRuntime``。

    容器归 Harbor 所有：这里不做导入导出、不停止容器，也不实现后台任务。
    所有上限仍来自宿主侧 ``SandboxPolicy``，模型传参不能放宽。
    """

    def __init__(self, environment: Any, loop: asyncio.AbstractEventLoop,
                 settings: Settings, *, python: str | None = "python3",
                 workspace_path: str = "/workspace",
                 policy: SandboxPolicy | None = None) -> None:
        """environment 只需实现 ``exec`` 与 ``upload_file`` 两个异步方法。"""
        self._environment = environment
        self._loop = loop
        self.settings = settings
        self._python = python
        self.workspace_path = workspace_path
        self.policy = policy or SandboxPolicy(background_enabled=False)
        self.directory = settings.workspace
        self._cancelled = threading.Event()
        self._request_path = Path(tempfile.mkdtemp(prefix="coder-tb-")) / "request.json"

    # ------------------------------------------------------------------
    # WorkspaceRuntime 协议
    # ------------------------------------------------------------------
    def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """与 DockerRuntime 相同的前置校验，再把请求交给容器内 Worker。"""
        if name not in SANDBOX_TOOLS:
            return ToolResult("", name, "Error: tool is not a sandbox workspace tool", True)
        if name in BACKGROUND_TOOLS:
            return ToolResult(
                "", name,
                "Error: background tasks are unavailable in the Terminal-Bench adapter", True)
        if self._cancelled.is_set():
            return ToolResult("", name, "Error: sandbox runtime was cancelled", True)
        supplied = arguments.get("timeout_s", self.settings.command_timeout_s)
        if name == "bash" and (type(supplied) is not int or not 1 <= supplied <= 86_400):
            return ToolResult("", name, "Error: timeout_s must be 1..86400 seconds", True)
        timeout_s = supplied if name == "bash" else self.settings.command_timeout_s
        if timeout_s > self.policy.max_command_timeout_s:
            return ToolResult("", name, "Error: timeout_s exceeds sandbox profile limit", True)
        if self._python is None:
            # 任务镜像没有 Python 时保留 bash；文件工具无法执行，必须显式说明。
            return self._shell_only(name, arguments, timeout_s)
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
            completed = self._submit(self._worker(request, timeout_s), timeout_s)
            stdout = completed.stdout or ""
            body = _between(stdout, RESULT_BEGIN, RESULT_END)
            detail = ((_between(stdout, ERROR_BEGIN, ERROR_END) or "").strip()
                      or (completed.stderr or "").strip()
                      or stdout.strip())[:500]
            if body is None or not body.strip():
                raise SandboxError(f"sandbox worker failed (exit {completed.return_code}): "
                                   f"{detail or 'no output'}")
            response = json.loads(body)
            if not isinstance(response, dict) or response.get("name") != name:
                raise SandboxError("sandbox worker returned an invalid response")
            content = response.get("content")
            error = response.get("is_error")
            if not isinstance(content, str) or not isinstance(error, bool):
                raise SandboxError("sandbox worker returned an invalid result")
            return ToolResult("", name, content, error)
        except (SandboxError, OSError, ValueError, RuntimeError) as exc:
            return ToolResult("", name, f"Error: {exc}", True)

    def cancel(self) -> None:
        """只阻止后续工具调用；容器生命周期由 Harbor 管理。"""
        self._cancelled.set()

    def close(self) -> None:
        self._cancelled.set()

    def get_task_state(self) -> str:
        """省掉每轮一次容器往返；计划本身仍在对话历史里。"""
        return ""

    def get_incomplete_tasks(self) -> dict[str, str]:
        return {}

    def get_export_issues(self) -> list[str]:
        """工作区不导出，交付预检不适用。"""
        return []

    def get_background_state(self) -> str:
        return ""

    def get_background_pending(self) -> tuple[str, ...]:
        return ()

    def acknowledge_background(self, job_ids: list[str]) -> None:
        return

    def background_job_ids(self) -> set[str]:
        return set()

    def interrupt_background(self, job_ids: set[str]) -> None:
        return

    # ------------------------------------------------------------------
    # 传输实现
    # ------------------------------------------------------------------
    def _submit(self, awaitable: Awaitable[_T], timeout_s: int) -> _T:
        """把协程交回 Harbor 的事件循环并阻塞等待结果。"""
        try:
            future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        except RuntimeError as exc:  # 事件循环已关闭
            raise SandboxError(f"sandbox runtime is unavailable: {exc}") from exc
        try:
            return future.result(timeout=timeout_s + _EXEC_MARGIN_S + _EXEC_GRACE_S)
        except TimeoutError as exc:
            future.cancel()
            raise SandboxError(f"sandbox tool timed out after {timeout_s}s") from exc

    async def _worker(self, request: bytes, timeout_s: int) -> Any:
        """上传请求文件，再运行一次容器内 Worker。"""
        self._request_path.write_bytes(request)
        await self._environment.upload_file(self._request_path, REQUEST_PATH)
        return await self._environment.exec(
            self._command(), timeout_sec=timeout_s + _EXEC_MARGIN_S,
            env={"CODER_EXPORT_EXCLUDE": _EXPORT_EXCLUDE,
                 # verifier 在 agent 结束后访问常驻服务，不能杀后台子进程。
                 "CODER_KEEP_BACKGROUND": "1"})

    def _command(self) -> str:
        """用哨兵包住 Worker 的 stdout/stderr，宿主只解析标记之间的内容。"""
        python = shlex.quote(self._python or "python3")
        return (
            f"{python} {WORKER_PATH} < {REQUEST_PATH} > {RESPONSE_PATH} 2> {ERROR_PATH}; "
            f"rc=$?; "
            f"printf '\\n{RESULT_BEGIN}\\n'; cat {RESPONSE_PATH}; "
            f"printf '\\n{RESULT_END}\\n'; "
            f"printf '{ERROR_BEGIN}\\n'; tail -c 4096 {ERROR_PATH}; "
            f"printf '\\n{ERROR_END}\\n'; exit $rc")

    def _shell_only(self, name: str, arguments: dict[str, Any], timeout_s: int) -> ToolResult:
        """任务镜像缺少 python3 时的降级：只保留 bash 直连。"""
        if name != "bash":
            return ToolResult("", name,
                              "Error: python3 is unavailable in the task container; "
                              "only bash is usable in this trial", True)
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult("", name, "Error: command must be a non-empty string", True)
        try:
            completed = self._submit(
                self._environment.exec(command, cwd=self.workspace_path,
                                       timeout_sec=timeout_s), timeout_s)
        except (SandboxError, OSError, RuntimeError) as exc:
            return ToolResult("", name, f"Error: {exc}", True)
        content = (f"exit code: {completed.return_code}\n"
                   f"stdout:\n{completed.stdout or ''}\n"
                   f"stderr:\n{completed.stderr or ''}")
        return ToolResult("", name, content, completed.return_code != 0)
