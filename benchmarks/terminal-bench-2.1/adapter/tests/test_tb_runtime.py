"""HarborRuntime 与 zipapp 构建的协议测试；不启动容器，也不依赖 Harbor 包。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from evals.tb.build_worker import build_worker_zip
from evals.tb.runtime import (
    ERROR_BEGIN,
    ERROR_END,
    REQUEST_PATH,
    RESULT_BEGIN,
    RESULT_END,
    HarborRuntime,
)

from coder.config import Settings


def _exec_result(stdout: str = "", stderr: str = "", return_code: int = 0):
    """Harbor ExecResult 的最小替身，避免测试导入 Harbor。"""
    return SimpleNamespace(stdout=stdout, stderr=stderr, return_code=return_code)


class FakeEnvironment:
    """记录 upload/exec，并可按请求生成哨兵包着的响应。"""

    def __init__(self, *, response: dict | None = None, stdout: str | None = None,
                 stderr: str = "", return_code: int = 0, shell=None) -> None:
        self.requests: list[bytes] = []
        self.targets: list[str] = []
        self.commands: list[str] = []
        self.timeouts: list[int | None] = []
        self.envs: list[dict[str, str] | None] = []
        self.cwds: list[str | None] = []
        self._response = response
        self._stdout = stdout
        self._stderr = stderr
        self._return_code = return_code
        self._shell = shell

    async def upload_file(self, source_path, target_path) -> None:
        self.requests.append(Path(source_path).read_bytes())
        self.targets.append(str(target_path))

    async def exec(self, command: str, cwd=None, env=None, timeout_sec=None,
                   user=None):
        self.commands.append(command)
        self.cwds.append(cwd)
        self.timeouts.append(timeout_sec)
        self.envs.append(env)
        if self._shell is not None:
            return self._shell
        if self._stdout is not None:
            return _exec_result(self._stdout, self._stderr, self._return_code)
        request = json.loads(self.requests[-1])
        response = self._response or {"name": request["name"], "content": "ok",
                                      "is_error": False}
        body = json.dumps(response)
        stdout = (f"docker compose warning: something\n"
                  f"{RESULT_BEGIN}\n{body}\n{RESULT_END}\n"
                  f"{ERROR_BEGIN}\n\n{ERROR_END}\n")
        return _exec_result(stdout, self._stderr, self._return_code)


@pytest.fixture
def loop():
    """Harbor 的事件循环在真实运行时属于主线程；测试用后台线程复现。"""
    event_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=event_loop.run_forever, daemon=True)
    thread.start()
    try:
        yield event_loop
    finally:
        event_loop.call_soon_threadsafe(event_loop.stop)
        thread.join(timeout=5)
        event_loop.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(model="test-model", api_key=None, base_url="http://localhost/v1",
                    workspace=tmp_path)


def _runtime(environment: FakeEnvironment, loop, settings: Settings,
             **kwargs) -> HarborRuntime:
    return HarborRuntime(environment, loop, settings, **kwargs)


def test_bash_request_matches_worker_protocol(loop, settings) -> None:
    environment = FakeEnvironment()
    runtime = _runtime(environment, loop, settings)

    result = runtime.execute_tool("bash", {"command": "pwd", "timeout_s": 120})

    assert result.content == "ok"
    assert result.is_error is False
    assert environment.targets == [REQUEST_PATH]
    assert environment.timeouts == [120 + 15]
    assert environment.envs == [{"CODER_EXPORT_EXCLUDE": '["*"]',
                                 "CODER_KEEP_BACKGROUND": "1"}]
    request = json.loads(environment.requests[0])
    assert request == {
        "name": "bash",
        "arguments": {"command": "pwd", "timeout_s": 120},
        "timeout_s": 120,
        "output_bytes": runtime.policy.command_output_bytes,
        "workspace_max_bytes": runtime.policy.workspace_max_bytes,
        "workspace_max_files": runtime.policy.workspace_max_files,
        "service_ports": [],
        "max_tool_output_chars": settings.max_tool_output_chars,
        "max_skill_chars": settings.max_skill_chars,
    }
    assert "worker.pyz" in environment.commands[0]
    assert "CODER_EXPORT_EXCLUDE" not in environment.commands[0]


def test_file_tool_timeout_comes_from_host_settings(loop, settings) -> None:
    environment = FakeEnvironment()
    runtime = _runtime(environment, loop, settings)

    runtime.execute_tool("read_file", {"path": "a.txt", "timeout_s": 999_999})

    request = json.loads(environment.requests[0])
    assert request["timeout_s"] == settings.command_timeout_s


def test_invalid_bash_timeout_is_rejected_without_exec(loop, settings) -> None:
    environment = FakeEnvironment()
    runtime = _runtime(environment, loop, settings)

    for value in (0, "60", 86_401):
        result = runtime.execute_tool("bash", {"command": "pwd", "timeout_s": value})
        assert result.is_error is True
        assert "timeout_s" in result.content
    assert environment.commands == []


def test_tools_outside_sandbox_are_rejected(loop, settings) -> None:
    environment = FakeEnvironment()
    runtime = _runtime(environment, loop, settings)

    result = runtime.execute_tool("memory_remember", {"text": "x"})

    assert result.is_error is True
    assert "not a sandbox workspace tool" in result.content
    assert environment.commands == []


def test_background_tools_are_rejected(loop, settings) -> None:
    environment = FakeEnvironment()
    runtime = _runtime(environment, loop, settings)

    result = runtime.execute_tool("start_background", {"command": "sleep 1"})

    assert result.is_error is True
    assert "background tasks are unavailable" in result.content
    assert environment.commands == []


def test_cancel_blocks_later_calls(loop, settings) -> None:
    environment = FakeEnvironment()
    runtime = _runtime(environment, loop, settings)

    runtime.cancel()

    result = runtime.execute_tool("bash", {"command": "pwd"})
    assert result.is_error is True
    assert "cancelled" in result.content
    assert environment.commands == []


def test_response_with_wrong_name_is_an_error(loop, settings) -> None:
    environment = FakeEnvironment(response={"name": "sandbox", "content": "x",
                                            "is_error": False})
    runtime = _runtime(environment, loop, settings)

    result = runtime.execute_tool("bash", {"command": "pwd"})

    assert result.is_error is True
    assert "invalid response" in result.content


def test_missing_sentinel_reports_exit_and_stderr(loop, settings) -> None:
    environment = FakeEnvironment(stdout="bash: python3: command not found", stderr="",
                                  return_code=127)
    runtime = _runtime(environment, loop, settings)

    result = runtime.execute_tool("bash", {"command": "pwd"})

    assert result.is_error is True
    assert "exit 127" in result.content
    assert "python3: command not found" in result.content


def test_python3_missing_degrades_to_bash(loop, settings) -> None:
    environment = FakeEnvironment(shell=_exec_result("hi\n", "", 0))
    runtime = _runtime(environment, loop, settings, python=None, workspace_path="/app")

    result = runtime.execute_tool("bash", {"command": "echo hi"})

    assert result.is_error is False
    assert "exit code: 0" in result.content
    assert "hi" in result.content
    assert environment.cwds == ["/app"]
    assert environment.targets == []

    denied = runtime.execute_tool("read_file", {"path": "a.txt"})
    assert denied.is_error is True
    assert "python3 is unavailable" in denied.content


def test_build_worker_zip_contains_runtime_and_dependencies(tmp_path: Path) -> None:
    archive = build_worker_zip(tmp_path / "worker.pyz", force=True)

    with zipfile.ZipFile(archive) as zipped:
        names = set(zipped.namelist())
        assert "__main__.py" in names
        assert "coder/sandbox/worker.py" in names
        assert "dotenv/__init__.py" in names
        assert "yaml/__init__.py" in names
        assert not any(name.endswith((".pyd", ".so", ".dll")) for name in names)
        # 容器内不能因为包初始化器而导入 tavily / web_search。
        tools_init = zipped.read("coder/tools/__init__.py").decode("utf-8")
        assert "web_search" not in tools_init
        # /workspace 可能是符号链接；入口必须先解析它，文件工具的
        # relative_to(ctx.workspace) 才能和 resolve_workspace_path 对齐。
        entry = zipped.read("__main__.py").decode("utf-8")
        assert "worker.WORKSPACE = Path(worker.WORKSPACE).resolve()" in entry
        assert "CODER_KEEP_BACKGROUND" in entry
        assert "worker._stop_command_descendants = lambda: None" in entry


def test_build_worker_zip_reuses_fresh_archive(tmp_path: Path) -> None:
    archive = build_worker_zip(tmp_path / "worker.pyz", force=True)
    built = archive.stat().st_mtime_ns

    assert build_worker_zip(tmp_path / "worker.pyz") == archive
    assert archive.stat().st_mtime_ns == built


def test_worker_archive_starts_without_installed_coder(tmp_path: Path) -> None:
    """在独立解释器中实际启动归档，验证打包后的导入与 JSON 协议。"""
    archive = build_worker_zip(tmp_path / "worker.pyz", force=True)
    completed = subprocess.run([sys.executable, "-I", str(archive)], input=b"",
                               capture_output=True, cwd=tmp_path, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    response = json.loads(completed.stdout)
    assert response["name"] == "sandbox"
    assert response["is_error"] is True
