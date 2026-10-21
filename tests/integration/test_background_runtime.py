"""真实 Docker 验收：并行、继续编辑、独立清理以及停止前日志归档。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coder.agent import Agent
from coder.config import Settings
from coder.conversation.session import save_session
from coder.permissions import DefaultPermissionPolicy
from coder.sandbox.base import SandboxError
from coder.sandbox.docker import DockerRuntime, cleanup_task, doctor
from coder.sandbox.policy import SandboxPolicy
from coder.tools import default_tools
from coder.types import LLMReply, Message, ToolCall


@pytest.fixture
def runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = SandboxPolicy(state_dir=tmp_path / "state", max_background_jobs=2,
                           command_output_bytes=4096)
    try:
        doctor(policy)
    except SandboxError as exc:
        pytest.skip(f"Docker background image unavailable: {exc}")
    active = DockerRuntime.start(Settings("scripted", None, None, workspace), policy)
    active.background.interval = 0.2
    active.background.notifier = lambda _: None
    try:
        yield active
    finally:
        if active.running:
            active.cancel()
        cleanup_task(policy, active.record["task_id"], discard=True)


def execute(runtime, name, arguments):
    result = runtime.execute_tool(name, arguments)
    assert not result.is_error, result.content
    return json.loads(result.content)


def test_parallel_edit_foreground_command_notifications_and_exit_archive(runtime):
    # 工作区中恶意同名包不能遮蔽 -I 启动的 Worker 和后台 runner。
    prepared = runtime.execute_tool("bash", {"command": "mkdir coder"})
    assert not prepared.is_error, prepared.content
    planted = runtime.execute_tool("write_file", {"path": "coder/__init__.py",
                                                  "content": "raise RuntimeError('workspace package imported')"})
    assert not planted.is_error
    first = execute(runtime, "start_background", {
        "command": "echo START_A; while [ ! -f release_a ]; do sleep 0.1; done; echo DONE_A",
        "timeout_s": 60,
    })["job_id"]
    second = execute(runtime, "start_background", {
        "command": "echo START_B; while [ ! -f release_b ]; do sleep 0.1; done; echo DONE_B",
        "timeout_s": 60,
    })["job_id"]
    assert execute(runtime, "get_background", {"job_id": first})["status"] == "running"
    assert execute(runtime, "get_background", {"job_id": second})["status"] == "running"
    rejected = runtime.execute_tool("start_background", {"command": "echo too many"})
    assert rejected.is_error and "concurrency" in rejected.content

    written = runtime.execute_tool("write_file", {"path": "foreground.txt", "content": "edit allowed"})
    assert not written.is_error, written.content
    foreground = runtime.execute_tool("bash", {"command": "touch release_a; sleep 2; cat foreground.txt"})
    assert not foreground.is_error and "edit allowed" in foreground.content
    # 监测器在前台长命令执行时仍可完成归档，而不等待该次 Worker 退出。
    assert runtime.background.jobs[first]["archived"]
    ended = execute(runtime, "wait_background", {"job_id": first})
    assert ended["status"] == "succeeded" and "DONE_A" in ended["output_tail"]
    assert runtime.execute_tool("cancel_background", {"job_id": first}).is_error is False
    assert execute(runtime, "get_background", {"job_id": second})["status"] == "running"

    # 取消 B 不影响容器或其他命令；取消本身以 is_error 提醒任务没有执行成功。
    cancelled = runtime.execute_tool("cancel_background", {"job_id": second})
    value = json.loads(cancelled.content)
    assert cancelled.is_error and value["status"] == "cancelled" and value["cleanup_confirmed"]
    assert runtime.running

    pending = execute(runtime, "start_background", {"command": "echo BEFORE_EXIT; sleep 30"})["job_id"]
    diff = runtime.export_changes()
    archived = json.loads((runtime.directory / "background" / pending / "result.json").read_text())
    assert archived["status"] == "cancelled" and archived["cleanup_confirmed"]
    assert b"BEFORE_EXIT" in (runtime.directory / "background" / pending / "output.head").read_bytes()
    assert "foreground.txt" in diff["added"]
    assert not any("coder-background" in path for paths in diff.values() for path in paths)
    assert not (runtime.settings.workspace / "foreground.txt").exists()


def test_timeout_cleans_detached_descendants_and_keeps_sibling_and_foreground_alive(runtime):
    sibling = execute(runtime, "start_background", {
        "command": "while [ ! -f release ]; do sleep 0.1; done; echo SIBLING_OK",
        "timeout_s": 60,
    })["job_id"]
    timed = execute(runtime, "start_background", {
        "command": "python -c 'import subprocess,time; "
                   "subprocess.Popen([\"/bin/sh\",\"-c\",\"sleep 4; echo leaked > orphan.txt\"], "
                   "start_new_session=True); time.sleep(30)'",
        "timeout_s": 1,
    })["job_id"]
    result = runtime.execute_tool("wait_background", {"job_id": timed})
    value = json.loads(result.content)
    assert result.is_error and value["status"] == "timed_out" and value["cleanup_confirmed"]
    foreground_timeout = runtime.execute_tool("bash", {"command": "sleep 30", "timeout_s": 1})
    assert foreground_timeout.is_error and "timed out" in foreground_timeout.content
    assert execute(runtime, "get_background", {"job_id": sibling})["status"] == "running"
    safe = runtime.execute_tool("bash", {"command": "sleep 3; test ! -e orphan.txt; touch release"})
    assert not safe.is_error, safe.content
    assert execute(runtime, "wait_background", {"job_id": sibling})["status"] == "succeeded"


def test_nonzero_exit_and_flooded_output_preserve_diagnostics_and_valid_json(runtime):
    identifier = execute(runtime, "start_background", {
        "command": "python -c 'import sys; print(\"BEGIN\"); "
                   "sys.stdout.write(\"x\"*2000000); print(\"TAIL_DIAGNOSTIC\"); sys.exit(7)'",
        "timeout_s": 30,
    })["job_id"]
    result = runtime.execute_tool("wait_background", {"job_id": identifier})
    value = json.loads(result.content)
    assert result.is_error and value["status"] == "failed" and value["exit_code"] == 7
    assert value["output_truncated"] and value["output_retained_bytes"] == 4096
    assert "TAIL_DIAGNOSTIC" in value["output_tail"]
    assert value["output_total_bytes"] > 2000000
    # 快速退出后查询不重启命令，也不把退出码误报为启动失败。
    again = runtime.execute_tool("get_background", {"job_id": identifier})
    assert json.loads(again.content)["exit_code"] == 7


def test_agent_background_round_trip_with_real_container(runtime):
    class ScriptedBackgroundModel:
        def __init__(self):
            self.requests = []

        def complete(self, messages, tools):
            self.requests.append(list(messages))
            step = len(self.requests)
            if step == 1:
                call = ToolCall("start", "start_background", {
                    "command": "echo RUNNER_OK; sleep 0.5; echo FINISHED", "timeout_s": 30})
            elif step == 2:
                call = ToolCall("write", "write_file", {"path": "agent-note.txt", "content": "parallel edit"})
            elif step == 3:
                started = next(json.loads(m.content) for m in messages
                               if m.role == "tool" and m.call_id == "start")
                call = ToolCall("wait", "wait_background", {"job_id": started["job_id"]})
            else:
                return LLMReply("Verified runner output.", Message("assistant", "Verified runner output."))
            return LLMReply("", Message("assistant", "", (call,)), (call,))

    model = ScriptedBackgroundModel()

    def checkpoint(history):
        # 与 CLI 一样注入保存回调，再检查后台结果已经写入会话检查点。
        save_session(runtime.directory, "history", runtime.settings, history,
                     agent.skill_state(), agent.context.memory_state())

    agent = Agent(model, default_tools(skill_catalog=runtime.skill_catalog),
                  DefaultPermissionPolicy(yes=True), runtime.settings, runtime,
                  history_checkpoint=checkpoint)
    result = agent.run("Run in background, edit an independent file, and inspect the result.")
    assert result.stop_reason == "completed" and len(model.requests) == 4
    assert all(not item.is_error for item in result.tool_results)
    assert "FINISHED" in result.tool_results[-1].content
    assert runtime.background.pending() == ()
    for view in model.requests:
        agent.context.validate_tool_message_groups(view)
    saved = json.loads((runtime.directory / "sessions" / "history.json").read_text(encoding="utf-8"))
    assert saved["history"][-1]["call_id"] == "wait"
