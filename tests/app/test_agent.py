from __future__ import annotations

from pathlib import Path

import pytest

from coder.agent import Agent
from coder.config import Settings
from coder.llm import ScriptedLLM
from coder.permissions import DefaultPermissionPolicy
from coder.sandbox.base import SandboxError
from coder.tools import default_tools
from coder.tools.base import ToolContext
from coder.tools.edit import EditFileTool, WriteFileTool
from coder.tools.read import ReadFileTool
from coder.tools.search import GlobTool, GrepTool
from coder.types import LLMReply, Message, ToolCall, ToolResult


class FakeDockerRuntime:
    """在单元测试中模拟 Worker 的文件工具路由，不启动宿主 bash。"""

    def __init__(self, settings: Settings) -> None:
        self.context = ToolContext(settings.workspace, settings.workspace, settings)
        self.incomplete_tasks: dict[str, str] = {}
        self.tools = {tool.name: tool for tool in
                      (ReadFileTool(), GlobTool(), GrepTool(), EditFileTool(), WriteFileTool())}

    def execute_tool(self, name: str, arguments: dict) -> ToolResult:
        if name == "bash":
            return ToolResult("", name, "simulated container command success")
        return self.tools[name].execute(arguments, self.context)

    def get_incomplete_tasks(self) -> dict[str, str]:
        return dict(self.incomplete_tasks)

    def get_task_state(self) -> str:
        return ""

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


def call(index: int, name: str, args: dict) -> LLMReply:
    item = ToolCall(str(index), name, args)
    return LLMReply("", Message("assistant", "", (item,)), (item,))


def final(text: str) -> LLMReply:
    return LLMReply(text, Message("assistant", text))


def agent(workspace: Path, replies: list[LLMReply], *, yes: bool = True,
          mode: str = "interactive", max_rounds: int = 12) -> tuple[Agent, ScriptedLLM]:
    model = ScriptedLLM(replies)
    settings = Settings("scripted", None, None, workspace, max_rounds=max_rounds)
    instance = Agent(model, default_tools(), DefaultPermissionPolicy(yes=yes),
                     settings, FakeDockerRuntime(settings), mode=mode)
    return instance, model


def test_final_without_tool(workspace: Path) -> None:
    instance, model = agent(workspace, [final("done")])
    result = instance.run("hello")
    assert result.stop_reason == "completed" and result.rounds == 1
    assert len(model.requests) == 1
    assert result.tool_call_counts == {}
    assert result.to_dict()["tool_call_counts"] == {}


@pytest.mark.parametrize("reason, expected", [
    ("length", "model_output_truncated"),
    ("content_filter", "model_content_filtered"),
    ("stream_incomplete", "model_response_incomplete"),
])
def test_interrupted_reply_does_not_execute_calls_or_claim_completion(workspace, reason, expected):
    tool = ToolCall("partial", "write_file", {"path": "must-not-exist", "content": "x"})
    partial = LLMReply("partial", Message("assistant", "partial", (tool,)),
                       (tool,), finish_reason=reason)
    instance, model = agent(workspace, [partial, final("finished after user continued")])
    result = instance.run("implement")
    assert result.stop_reason == "incomplete" and result.incomplete_reasons == (expected,)
    assert result.text == "partial" and result.tool_call_counts == {}
    assert not (workspace / "must-not-exist").exists()
    assert not result.history[-1].tool_calls
    instance.context.validate_tool_message_groups(result.history)
    assert len(model.requests) == 1
    assert instance.run("continue").stop_reason == "completed"


def test_final_reply_cannot_hide_unexportable_files(workspace, monkeypatch):
    instance, _ = agent(workspace, [final("done")])
    problems = ["file exceeds export limit: weights.bin"]
    monkeypatch.setattr(instance.runtime, "get_export_issues", lambda: problems, raising=False)
    result = instance.run("finish the project")
    assert result.stop_reason == "incomplete"
    assert "workspace_unexportable" in result.incomplete_reasons
    assert result.export_issues == tuple(problems)


def test_pending_task_yields_to_user_without_another_model_round(workspace: Path) -> None:
    text = "Please provide the model service address."
    instance, model = agent(workspace, [final(text)])
    instance.runtime.incomplete_tasks = {"task_a1b2c3d4": "pending"}
    result = instance.run("Build an app; ask me for any missing configuration")
    assert result.stop_reason == "incomplete"
    assert result.text == text and result.rounds == 1
    assert len(model.requests) == 1
    assert [m.content for m in result.history if m.role == "user"] == [
        "Build an app; ask me for any missing configuration"
    ]


def test_task_query_failure_does_not_report_completion(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _ = agent(workspace, [final("done")])

    def fail_query() -> dict[str, str]:
        raise SandboxError("sandbox task query returned invalid JSON")

    monkeypatch.setattr(instance.runtime, "get_incomplete_tasks", fail_query)
    result = instance.run("build an app")
    assert result.stop_reason == "failed"
    assert "task query returned invalid JSON" in result.text


def test_task_state_refreshes_after_tools_without_polluting_history(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coder.tools.task as tasks
    monkeypatch.setattr(tasks, "WORKDIR", workspace)
    monkeypatch.setattr(tasks, "TASKS", tasks.TaskStore(workspace / ".tasks"))
    instance, model = agent(workspace, [
        call(1, "create_task", {"subject": "Implement the backend"}),
        final("Please provide the model address."),
    ])
    monkeypatch.setattr(instance.runtime, "get_task_state", tasks.task_state)
    monkeypatch.setattr(instance.runtime, "get_incomplete_tasks", tasks.incomplete_tasks)
    monkeypatch.setattr(instance.runtime, "execute_tool",
                        lambda name, arguments: tasks.create_task().execute(
                            arguments, instance.runtime.context))
    result = instance.run("Build an application")
    assert result.stop_reason == "incomplete" and len(model.requests) == 2
    assert not any(message.content.startswith("Current task state:\n") for message in model.requests[0])
    assert any(message.content.startswith("Current task state:\n") and "Implement the backend" in message.content
               for message in model.requests[1])
    assert not any(message.content.startswith("Current task state:\n") for message in result.history)


def test_empty_model_answer_retries_without_claiming_success(workspace: Path) -> None:
    instance, model = agent(workspace, [final(""), final("done")])
    result = instance.run("hello")
    assert result.stop_reason == "completed" and result.rounds == 2
    assert "previous assistant response was empty" in model.requests[1][-1].content
    limited, _ = agent(workspace, [final("")], max_rounds=1)
    assert limited.run("hello").stop_reason == "round_limit"


def test_offline_fix_chain(workspace: Path) -> None:
    replies = [call(1, "glob", {"pattern": "**/*.py"}),
               call(2, "grep", {"pattern": "def divide"}),
               call(3, "read_file", {"path": "calculator.py"}),
               call(4, "edit_file", {"path": "calculator.py",
                                      "old_text": "    if b == 0:\n        return None",
                                      "new_text": "    if b == 0:\n        raise ZeroDivisionError()"}),
               call(5, "edit_file", {"path": "parse_config.py",
                                      "old_text": "    return value.strip()",
                                      "new_text": "    return value.strip() if value is not None else ''"}),
               call(6, "bash", {"command": "python -m pytest -q"}),
               final("Changed calculator.py and parse_config.py; requested verification.")]
    instance, model = agent(workspace, replies)
    outcome = instance.run("repair and verify")
    assert outcome.stop_reason == "completed"
    assert [item.name for item in outcome.tool_results] == ["glob", "grep", "read_file",
                                                            "edit_file", "edit_file", "bash"]
    assert outcome.tool_call_counts == {
        "glob": 1, "grep": 1, "read_file": 1, "edit_file": 2, "bash": 1,
    }
    assert all(not item.is_error for item in outcome.tool_results)
    assert outcome.tool_results[-1].content == "simulated container command success"
    assert [message.call_id for message in outcome.history if message.role == "tool"] == [str(i) for i in range(1, 7)]
    assert model.requests[-1][-1].call_id == "6"
    assert "raise ZeroDivisionError" in (workspace / "calculator.py").read_text()


def test_permission_denial_and_round_limit(workspace: Path) -> None:
    replies = [call(1, "edit_file", {"path": "calculator.py", "old_text": "x", "new_text": "y"}),
               final("I could not edit because permission was denied.")]
    before = (workspace / "calculator.py").read_text()
    instance, model = agent(workspace, replies, yes=False)
    outcome = instance.run("fix")
    assert outcome.tool_results[0].is_error
    assert outcome.tool_call_counts == {"edit_file": 1}
    assert "denied" in model.requests[1][-1].content
    assert (workspace / "calculator.py").read_text() == before
    limited, _ = agent(workspace, [call(1, "glob", {"pattern": "*.py"})], max_rounds=1)
    limited_result = limited.run("search")
    assert limited_result.stop_reason == "round_limit"
    assert limited_result.tool_call_counts == {"glob": 1}


def test_tool_call_and_result_logs_include_success_and_denial(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance, _ = agent(workspace, [
        call(1, "glob", {"pattern": "*.py"}),
        call(2, "edit_file", {"path": "calculator.py", "old_text": "x", "new_text": "y"}),
        final("done"),
    ], yes=False)
    instance.run("inspect and edit")
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith('[工具调用] "glob" 参数=')
    assert lines[1].startswith('[工具结果] "glob" 状态=成功 内容=')
    assert lines[2].startswith('[工具调用] "edit_file" 参数=')
    assert lines[3].startswith('[工具结果] "edit_file" 状态=失败 内容=')
    assert all(" id=" not in line for line in lines)
    assert "denied" in lines[3]
    assert captured.out == ""


def test_unknown_tool_and_script_exhaustion(workspace: Path) -> None:
    instance, model = agent(workspace, [call(1, "missing", {}), final("unknown")])
    unknown_result = instance.run("try")
    assert unknown_result.tool_results[0].is_error
    assert unknown_result.tool_call_counts == {"missing": 1}
    exhausted, _ = agent(workspace, [])
    exhausted_result = exhausted.run("try")
    assert exhausted_result.stop_reason == "failed"
    assert exhausted_result.tool_call_counts == {}


def test_registered_tool_without_docker_route_cannot_run_on_host(workspace: Path) -> None:
    class UnroutedTool:
        name = "host_write"
        is_read_only = True

        def schema(self) -> dict:
            return {"type": "function", "function": {"name": self.name}}

        def execute(self, arguments: dict, ctx: ToolContext) -> ToolResult:
            raise AssertionError("host tool must not run")

    settings = Settings("scripted", None, None, workspace)
    tools = default_tools()
    tools.register(UnroutedTool())
    instance = Agent(ScriptedLLM([]), tools, DefaultPermissionPolicy(), settings,
                     FakeDockerRuntime(settings))
    result = instance._execute_call(ToolCall("call-1", "host_write", {}))
    assert result.is_error
    assert "no Docker execution route" in result.content


def test_multi_tool_order(workspace: Path) -> None:
    a = ToolCall("a", "glob", {"pattern": "*.py"})
    b = ToolCall("b", "read_file", {"path": "calculator.py"})
    first = LLMReply("", Message("assistant", "", (a, b)), (a, b))
    instance, _ = agent(workspace, [first, final("done")])
    assert [x.call_id for x in instance.run("inspect").tool_results] == ["a", "b"]


def test_interrupt_completes_tool_group(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = ToolCall("a", "glob", {"pattern": "*.py"})
    b = ToolCall("b", "read_file", {"path": "calculator.py"})
    first = LLMReply("", Message("assistant", "", (a, b)), (a, b))
    instance, _ = agent(workspace, [first])
    original = instance._execute_call

    def interrupt(call: ToolCall):
        if call.id == "a":
            raise KeyboardInterrupt
        return original(call)

    instance._execute_call = interrupt  # type: ignore[method-assign]
    result = instance.run("inspect")
    assert result.stop_reason == "interrupted"
    assert [message.call_id for message in result.history if message.role == "tool"] == ["a", "b"]
    assert all(item.is_error for item in result.tool_results)
    assert result.tool_call_counts == {"glob": 1}
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith('[工具调用] "glob" 参数=')
    assert lines[1].startswith('[工具结果] "glob" 状态=失败')
    assert lines[2].startswith('[工具结果] "read_file" 状态=失败')
    assert all(" id=" not in line for line in lines)
    assert "Skipped because run was interrupted" in lines[2]


def test_tool_counts_reset_between_tasks_and_survive_model_failure(workspace: Path) -> None:
    instance, _ = agent(workspace, [
        call(1, "glob", {"pattern": "*.py"}),
        call(2, "glob", {"pattern": "*.txt"}),
        final("first task done"),
        call(3, "read_file", {"path": "missing.txt"}),
    ])
    first = instance.run("inspect files")
    assert first.tool_call_counts == {"glob": 2}
    assert first.to_dict()["tool_call_counts"] == {"glob": 2}

    # 第二个任务读文件失败后，模型脚本耗尽；保留已发生的调用，不累计上一任务。
    second = instance.run("read a missing file")
    assert second.stop_reason == "failed"
    assert second.tool_results[0].is_error
    assert second.tool_call_counts == {"read_file": 1}
    assert first.tool_call_counts == {"glob": 2}
