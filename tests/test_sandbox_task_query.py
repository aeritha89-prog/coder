"""验证任务查询的进程状态、JSON 边界和失败清理，不需要启动 Docker。"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import Mock

import pytest

from study_coder.sandbox.base import SandboxError
from study_coder.sandbox.docker import DockerRuntime


@pytest.fixture
def query_process(monkeypatch: pytest.MonkeyPatch) -> Mock:
    process = Mock()
    process.returncode = 0
    process.communicate.return_value = (b"{}\n", b"")
    monkeypatch.setattr("study_coder.sandbox.docker._docker_executable", lambda: "docker")
    monkeypatch.setattr("study_coder.sandbox.docker.subprocess.Popen", Mock(return_value=process))
    return process


def query_runtime() -> DockerRuntime:
    runtime = object.__new__(DockerRuntime)
    runtime.container = "test-container"
    return runtime


@pytest.mark.parametrize("output, expected", [
    (b"{}\n", {}),
    (b" \n {} \r\n", {}),
    (b'{"task_a1b2c3d4": "pending", "task_11223344": "in_progress"}',
     {"task_a1b2c3d4": "pending", "task_11223344": "in_progress"}),
])
def test_valid_task_query_returns_structured_state(
    query_process: Mock, output: bytes, expected: dict[str, str],
) -> None:
    query_process.communicate.return_value = (output, b"diagnostic warning\n")
    assert query_runtime().get_incomplete_tasks() == expected
    query_process.communicate.assert_called_once_with(timeout=10)


@pytest.mark.parametrize("output", [
    b"", b"{", b"\xff", b"null", b"[]",
    b'{"task_invalid": "pending"}',
    b'{"task_a1b2c3d4": "completed"}',
    b'{"task_a1b2c3d4": false}',
    b'{"task_a1b2c3d4": ["pending"]}',
])
def test_invalid_task_query_is_not_treated_as_empty(query_process: Mock, output: bytes) -> None:
    query_process.communicate.return_value = (output, b"")
    with pytest.raises(SandboxError, match="task query"):
        query_runtime().get_incomplete_tasks()


@pytest.mark.parametrize("output, stderr", [(b"", b""), (b"{}", b"Docker failure")])
def test_failed_process_cannot_report_success(
    query_process: Mock, output: bytes, stderr: bytes,
) -> None:
    query_process.returncode = 1
    query_process.communicate.return_value = (output, stderr)
    with pytest.raises(SandboxError, match=r"task query failed \(exit 1\)"):
        query_runtime().get_incomplete_tasks()


def test_timeout_kills_and_reaps_query_process(query_process: Mock) -> None:
    query_process.communicate.side_effect = [subprocess.TimeoutExpired("docker", 10), (b"", b"")]
    with pytest.raises(SandboxError, match="timed out after 10s"):
        query_runtime().get_incomplete_tasks()
    query_process.kill.assert_called_once_with()
    assert query_process.communicate.call_count == 2


def test_interrupt_kills_query_and_remains_an_interrupt(query_process: Mock) -> None:
    query_process.communicate.side_effect = [KeyboardInterrupt(), (b"", b"")]
    with pytest.raises(KeyboardInterrupt):
        query_runtime().get_incomplete_tasks()
    query_process.kill.assert_called_once_with()


def test_query_pins_workspace_and_isolated_python(query_process: Mock) -> None:
    query_runtime().get_incomplete_tasks()
    from study_coder.sandbox import docker
    args = docker.subprocess.Popen.call_args.args[0]
    assert args[:5] == ["docker", "exec", "--workdir", "/workspace", "test-container"]
    assert args[5:9] == ["/opt/study-coder/.venv/bin/python", "-I", "-B", "-c"]


def test_process_start_failure_is_a_sandbox_error(
    query_process: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("study_coder.sandbox.docker.subprocess.Popen", Mock(side_effect=OSError("missing")))
    with pytest.raises(SandboxError, match="task query could not start"):
        query_runtime().get_incomplete_tasks()


@pytest.mark.parametrize("state", ["", '{"tasks": []}', "任务摘要"])
def test_context_query_returns_a_string(query_process: Mock, state: str) -> None:
    query_process.communicate.return_value = (json.dumps(state).encode(), b"")
    assert query_runtime().get_task_state() == state


@pytest.mark.parametrize("state", [{}, None, "x" * 2001])
def test_invalid_context_query_is_rejected(query_process: Mock, state) -> None:
    query_process.communicate.return_value = (json.dumps(state).encode(), b"")
    with pytest.raises(SandboxError, match="invalid context state"):
        query_runtime().get_task_state()
