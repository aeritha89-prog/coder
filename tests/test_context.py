from __future__ import annotations

from pathlib import Path

import pytest

from study_coder.agent import Agent
from study_coder.config import Settings
from study_coder.context import ContextError, ContextManager, ContextSummary, ModelSummarizer
from study_coder.llm import ScriptedLLM
from study_coder.permissions import DefaultPermissionPolicy
from study_coder.session import load_session_bundle, save_session
from study_coder.tools.base import ToolContext, ToolRegistry
from study_coder.tools.read import ReadFileTool
from study_coder.types import LLMReply, Message, ToolCall, ToolResult, Usage


class FakeReadRuntime:
    """只模拟容器内读取，供上下文压缩测试观察真实长度的结果。"""

    def __init__(self, settings: Settings) -> None:
        self.context = ToolContext(settings.workspace, settings.workspace, settings)

    def execute_tool(self, name: str, arguments: dict) -> ToolResult:
        assert name == "read_file"
        return ReadFileTool().execute(arguments, self.context)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_incomplete_tasks(self) -> dict[str, str]:
        return {}

    def get_task_state(self) -> str:
        return ""


def read_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    return registry


def history_with_calls(count: int, result_size: int = 300) -> list[Message]:
    history = [Message("system", "Policy"), Message("user", "Fix the calculator bug")]
    for number in range(count):
        call = ToolCall(f"id-{number}", "read_file", {"path": f"file-{number}.py"})
        history.append(Message("assistant", "", (call,)))
        history.append(Message("tool", f"file-{number}.py: " + "x" * result_size,
                               call_id=call.id))
    return history


class FakeSummarizer:
    def __init__(self, text: str = "Earlier files inspected; calculator.py still needs a fix.",
                 fail: bool = False, usage: Usage | None = None) -> None:
        self.text = text
        self.fail = fail
        self.usage = usage
        self.calls: list[tuple[list[Message], str]] = []

    def summarize(self, messages, previous: str = "") -> ContextSummary:
        self.calls.append((list(messages), previous))
        if self.fail:
            raise RuntimeError("summary service unavailable")
        return ContextSummary(self.text, self.usage)


def test_tool_output_snip_does_not_change_durable_history(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=3_000)
    manager = ContextManager(settings)
    original = history_with_calls(1, result_size=7_000)
    view = manager.build_view(original)
    assert original[-1].content.endswith("x" * 7_000)
    assert "[snipped" in view[-1].content
    assert view[1] is original[1]
    assert view[-1].call_id == original[-1].call_id
    manager.validate_tool_message_groups(view)
    assert "[上下文压缩] 裁剪工作上下文的工具输出" in capsys.readouterr().err


def test_summary_preserves_complete_exchanges_and_reuses_cache(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    summarizer = FakeSummarizer()
    manager = ContextManager(settings, summarizer)
    original = history_with_calls(8)
    view = manager.build_view(original)
    assert any("Earlier conversation summary" in item.content for item in view)
    assert original[1] in view  # the current user task is never rewritten
    assert original[-2:] == view[-2:]  # most recent assistant request + result stay paired
    assert len(view) < len(original)
    manager.validate_tool_message_groups(view)
    assert len(summarizer.calls) == 1
    first_logs = capsys.readouterr().err
    assert "[上下文压缩] 开始摘要" in first_logs
    assert "[上下文压缩] 摘要完成" in first_logs
    assert "[上下文压缩] 工作上下文摘要" in first_logs
    assert manager.build_view(original) == view
    assert len(summarizer.calls) == 1
    cached_logs = capsys.readouterr().err
    assert "[上下文压缩] 复用已有摘要" in cached_logs
    assert "开始摘要" not in cached_logs
    manager.build_view(history_with_calls(9))
    assert len(summarizer.calls) == 1  # 工作上下文未超限，不为每一组新结果调用摘要。
    manager.build_view(history_with_calls(17))
    assert len(summarizer.calls) == 2
    assert summarizer.calls[1][1] == summarizer.text
    # 首次覆盖 user + 前 6 个调用；第二次只处理调用 6..14。
    assert len(summarizer.calls[1][0]) == 18
    assert {message.call_id for message in summarizer.calls[1][0] if message.role == "tool"} == {
        f"id-{index}" for index in range(6, 15)}


def test_summary_failure_falls_back_without_orphan_messages(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    manager = ContextManager(settings, FakeSummarizer(fail=True))
    view = manager.build_view(history_with_calls(8))
    assert any("tool request read_file" in item.content for item in view)
    manager.validate_tool_message_groups(view)
    assert "模型摘要失败，已改用规则摘要" in capsys.readouterr().err


def test_two_tool_calls_remain_atomic_after_compression(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    manager = ContextManager(settings, FakeSummarizer())
    history = history_with_calls(8)
    first = ToolCall("multi-a", "grep", {"pattern": "divide"})
    second = ToolCall("multi-b", "read_file", {"path": "calculator.py"})
    history[-2:-2] = [Message("assistant", "", (first, second)),
                      Message("tool", "grep output", call_id="multi-a"),
                      Message("tool", "file output", call_id="multi-b")]
    view = manager.build_view(history)
    manager.validate_tool_message_groups(view)
    index = next(index for index, item in enumerate(view)
                 if item.role == "assistant" and item.tool_calls == (first, second))
    assert [item.call_id for item in view[index + 1:index + 3]] == ["multi-a", "multi-b"]


@pytest.mark.parametrize("task_state", [
    "Fix calculator; test pending",
    '{"counts":{"pending":1},"tasks":[{"id":"task_12345678","subject":"Fix calculator"}]}',
])
def test_hard_collapse_keeps_task_state_and_latest_exchange(
    workspace: Path, capsys: pytest.CaptureFixture[str], task_state: str,
) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    manager = ContextManager(settings, FakeSummarizer(text="S" * 5_000))
    original = history_with_calls(8)
    view = manager.build_view(original, task_state=task_state)
    assert any(item.content == "Current task state:\n" + task_state for item in view)
    assert any("Earlier work (compressed)" in item.content for item in view)
    assert original[1] in view
    assert view[-2].tool_calls[0].id == view[-1].call_id
    assert manager.estimate_tokens(view) < 0.9 * settings.context_window_tokens
    manager.validate_tool_message_groups(view)
    logs = capsys.readouterr().err
    assert "开始最终压缩" in logs
    assert "最终压缩完成" in logs


def test_invalid_groups_and_oversize_current_task_fail_clearly(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=500)
    manager = ContextManager(settings)
    call_a = ToolCall("a", "read_file", {"path": "a.py"})
    call_b = ToolCall("b", "read_file", {"path": "b.py"})
    with pytest.raises(ContextError, match="matching results"):
        manager.validate_tool_message_groups([
            Message("assistant", "", (call_a, call_b)), Message("tool", "a", call_id="a")])
    with pytest.raises(ContextError, match="orphan"):
        manager.validate_tool_message_groups([Message("tool", "a", call_id="a")])
    with pytest.raises(ContextError, match="exceed the context window"):
        manager.build_view([Message("system", "Policy"), Message("user", "X" * 4_000)])
    model = ScriptedLLM([])
    agent = Agent(model, read_tools(), DefaultPermissionPolicy(), settings,
                  FakeReadRuntime(settings))
    result = agent.run("X" * 4_000)
    assert result.stop_reason == "failed"
    assert "context window" in result.text
    assert model.requests == []


def test_agent_and_session_retain_full_history_after_model_view_is_snipped(workspace: Path) -> None:
    (workspace / "large.txt").write_text("x" * 8_000, encoding="utf-8")
    settings = Settings("scripted", None, None, workspace, context_window_tokens=2_000)
    call = ToolCall("read-1", "read_file", {"path": "large.txt"})
    model = ScriptedLLM([
        LLMReply("", Message("assistant", "", (call,)), (call,)),
        LLMReply("I read the file.", Message("assistant", "I read the file.")),
    ])
    agent = Agent(model, read_tools(), DefaultPermissionPolicy(), settings,
                  FakeReadRuntime(settings))
    result = agent.run("Read large.txt")
    assert result.stop_reason == "completed"
    durable_tool = next(item for item in agent.history if item.role == "tool")
    model_tool = next(item for item in model.requests[1] if item.role == "tool")
    assert len(durable_tool.content) > 8_000
    assert "[snipped" in model_tool.content
    save_session(workspace, "full_history", settings, agent.history)
    assert next(item for item in load_session_bundle(workspace, "full_history", settings)[0]
                if item.role == "tool").content == durable_tool.content


def test_model_summarizer_processes_all_chunks_and_counts_usage() -> None:
    model = ScriptedLLM([
        LLMReply("first part", Message("assistant", "first part"), usage=Usage(10, 3)),
        LLMReply("first and last part", Message("assistant", "first and last part"),
                 usage=Usage(12, 4)),
    ])
    summary = ModelSummarizer(model).summarize([Message("user", "A" * 13_000 + "TAIL")])
    assert len(model.requests) == 2
    assert "TAIL" in model.requests[1][-1].content
    assert "first part" in model.requests[1][-1].content
    assert summary.text == "first and last part"
    assert summary.usage == Usage(22, 7)


def test_agent_sends_summary_only_to_model_and_counts_its_usage(workspace: Path) -> None:
    (workspace / "medium.txt").write_text("x" * 320, encoding="utf-8")
    settings = Settings("scripted", None, None, workspace, context_window_tokens=1_400)
    summarizer = FakeSummarizer(text="Read medium.txt; task remains open.", usage=Usage(5, 2))
    calls = [ToolCall(f"read-{index}", "read_file", {"path": "medium.txt"})
             for index in range(8)]
    replies = [LLMReply("", Message("assistant", "", (call,)), (call,)) for call in calls]
    replies.append(LLMReply("Finished reading.", Message("assistant", "Finished reading.")))
    model = ScriptedLLM(replies)
    context = ContextManager(settings, summarizer)
    agent = Agent(model, read_tools(), DefaultPermissionPolicy(), settings,
                  FakeReadRuntime(settings), context=context)
    result = agent.run("Read medium.txt repeatedly")
    assert result.stop_reason == "completed"
    assert summarizer.calls
    assert any("Read medium.txt; task remains open." in message.content
               for request in model.requests for message in request)
    assert not any("Earlier conversation summary" in message.content for message in agent.history)
    assert agent.total_usage.prompt_tokens >= 5
    for request in model.requests:
        context.validate_tool_message_groups(request)


def test_small_new_messages_do_not_resummarize_large_archive(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=2_000)
    summarizer = FakeSummarizer(text="ARCHIVED_PROGRESS", usage=Usage(10, 2))
    manager = ContextManager(settings, summarizer)
    history = history_with_calls(50, result_size=300)
    original = list(history)
    first_view = manager.build_view(history)
    assert len(summarizer.calls) == 1
    assert manager.last_summary_usage == Usage(10, 2)
    assert manager.estimate_tokens(history) > settings.context_window_tokens
    for number in range(50, 55):
        history.extend(history_with_calls(number + 1)[-2:])
        view = manager.build_view(history)
        assert len(summarizer.calls) == 1
        assert manager.last_summary_usage is None
        assert "ARCHIVED_PROGRESS" in view[1].content
        assert history[1] in view
        manager.validate_tool_message_groups(view)
    assert len(view) > len(first_view)
    assert history[:len(original)] == original


def test_incremental_summary_never_resends_already_covered_calls(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    summarizer = FakeSummarizer(text="PROGRESS")
    manager = ContextManager(settings, summarizer)
    for count in range(8, 30):
        manager.build_view(history_with_calls(count))
    assert len(summarizer.calls) >= 2
    seen: set[str] = set()
    for messages, previous in summarizer.calls:
        manager.validate_tool_message_groups(messages)
        ids = {message.call_id for message in messages if message.role == "tool"}
        assert not (ids & seen)
        if seen:
            assert previous == "PROGRESS"
        seen.update(ids)


def test_new_user_task_is_kept_and_old_task_stays_in_summary(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    summarizer = FakeSummarizer(text="OLD_TASK_PROGRESS")
    manager = ContextManager(settings, summarizer)
    history = history_with_calls(8)
    manager.build_view(history)
    new_task = Message("user", "Now inspect another file")
    history.append(new_task)
    view = manager.build_view(history)
    assert new_task in view and history[1] not in view
    assert any("OLD_TASK_PROGRESS" in message.content for message in view)
    assert len(summarizer.calls) == 1


@pytest.mark.parametrize("change", ["modify", "truncate", "clear"])
def test_changed_history_invalidates_summary_cache(workspace: Path, change: str) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    summarizer = FakeSummarizer(text="STALE_PROGRESS")
    manager = ContextManager(settings, summarizer)
    history = history_with_calls(8)
    manager.build_view(history)
    if change == "modify":
        history = [history[0], Message("user", "Changed goal")]
    elif change == "truncate":
        history = history[:4]
    else:
        manager.reset()
        history = [history[0], Message("user", "Fresh task")]
    view = manager.build_view(history)
    assert not any("STALE_PROGRESS" in message.content for message in view)
    assert manager._summary_keys == ()
    assert manager.last_summary_usage is None
    manager.validate_tool_message_groups(view)


def test_failed_incremental_summary_retains_previous_progress(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=2_000)
    summarizer = FakeSummarizer(text="KEEP_EARLY_PROGRESS")
    manager = ContextManager(settings, summarizer)
    manager.build_view(history_with_calls(30, result_size=500))
    covered = len(manager._summary_keys)
    summarizer.fail = True
    history = history_with_calls(55, result_size=500)
    view = manager.build_view(history)
    assert "KEEP_EARLY_PROGRESS" in manager._summary_text
    delta = summarizer.calls[-1][0]
    assert all(int(message.call_id.split("-")[-1]) >= covered - 1
               for message in delta if message.role == "tool")
    assert any("KEEP_EARLY_PROGRESS" in message.content for message in view)
    manager.validate_tool_message_groups(view)
    count = len(summarizer.calls)
    manager.build_view(history)
    assert len(summarizer.calls) == count


def test_hard_compressed_summary_is_cached_without_recalling_model(workspace: Path) -> None:
    settings = Settings("scripted", None, None, workspace, context_window_tokens=800)
    summarizer = FakeSummarizer(text="S" * 5_000)
    manager = ContextManager(settings, summarizer)
    history = history_with_calls(8)
    manager.build_view(history)
    assert len(manager._summary_text) < 5_000
    count = len(summarizer.calls)
    view = manager.build_view(history_with_calls(9))
    assert len(summarizer.calls) == count
    assert manager.estimate_tokens(view) < 0.9 * settings.context_window_tokens
    manager.validate_tool_message_groups(view)
