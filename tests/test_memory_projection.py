"""模型实际输入回归：历史脱敏不能生成伪工具调用，也不能吞掉结果语义。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from study_coder.agent import Agent
from study_coder.config import Settings
from study_coder.context import ContextManager
from study_coder.llm import ScriptedLLM
from study_coder.memory import MEMORY_TOOLS, MemoryManager, MemoryStore
from study_coder.memory_view import MEMORY_OBSERVATION_HEADER
from study_coder.permissions import DefaultPermissionPolicy
from study_coder.tools import default_tools
from study_coder.types import LLMReply, Message, ToolCall


def setup(root: Path) -> tuple[MemoryStore, MemoryManager]:
    store = MemoryStore(root / "state", root)
    return store, MemoryManager(store, auto_episode=False)


def records(messages: list[Message]) -> list[dict]:
    return [record for message in messages if message.content.startswith(MEMORY_OBSERVATION_HEADER)
            for record in json.loads(message.content[len(MEMORY_OBSERVATION_HEADER):])]


def group(call: ToolCall, result: str) -> list[Message]:
    return [Message("assistant", "", (call,)), Message("tool", result, call_id=call.id)]


@pytest.mark.parametrize("name", ["memory_search", "memory_remember", "memory_read", "memory_forget"])
def test_memory_history_has_no_fake_executable_arguments_and_does_not_mutate_source(tmp_path: Path, name: str) -> None:
    store, manager = setup(tmp_path)
    entry = store.remember("semantic", "private-key", "private original preference")
    fields = {"memory_search": {"query": "private-key"},
              "memory_remember": {"kind": "semantic", "key": "private-key", "content": entry.content},
              "memory_read": {"id": entry.id}, "memory_forget": {"id": entry.id}}[name]
    payload = {"memory_search": {"memories": [asdict(entry)], "omitted": 0},
               "memory_remember": {**asdict(entry), "saved": True},
               "memory_read": {"memory": asdict(entry)}, "memory_forget": {"deleted": True}}[name]
    history = group(ToolCall("past", name, fields), json.dumps(payload))
    original = repr(history)
    store.forget(entry.id)
    projected = manager.projection.project_messages(history)
    assert records(projected)[0]["tool"] == name
    assert not any(message.tool_calls or message.role == "tool" for message in projected)
    assert "historical_input" not in repr(projected) and "private original preference" not in repr(projected)
    assert "private-key" not in repr(projected) and repr(history) == original
    ContextManager(Settings("test", None, None, tmp_path)).validate_tool_message_groups(projected)


@pytest.mark.parametrize("omitted", [0, 3])
def test_empty_search_preserves_empty_success_and_omission_count(tmp_path: Path, omitted: int) -> None:
    _, manager = setup(tmp_path)
    call = ToolCall("empty", "memory_search", {"query": ""})
    for summary in (False, True):
        view = manager.projection.project_messages(group(call, json.dumps({"memories": [], "omitted": omitted})), summary=summary)
        assert records(view)[0]["result"] == {"memories": [], "omitted": omitted}


def test_missing_entry_and_invalid_reference_remain_distinct_from_call_failure(tmp_path: Path) -> None:
    _, manager = setup(tmp_path)
    call = ToolCall("read", "memory_read", {"id": "mem_" + "a" * 16})
    missing = manager.projection.project_messages(group(call, '{"memory":null}'))
    assert records(missing)[0]["result"] == {"memory": None}
    invalid = manager.projection.project_messages(group(call, '{"memory":{"content":"withdrawn body"}}'))
    assert records(invalid)[0]["result"]["state"] == "unavailable"
    assert "withdrawn body" not in repr(invalid)
    error = manager.projection.project_messages(group(call, "Error: unexpected memory argument"))
    assert records(error)[0]["result"] == {"state": "error", "error": "Error: unexpected memory argument"}


def test_unknown_diagnostic_is_still_failure_without_reintroducing_old_body(tmp_path: Path) -> None:
    _, manager = setup(tmp_path)
    call = ToolCall("bad", "memory_remember", {"historical_input": "old malformed input"})
    for summary in (False, True):
        view = manager.projection.project_messages(group(call, "Error: withdrawn-private-content"), summary=summary)
        assert records(view)[0]["result"]["state"] == "error"
        assert "withdrawn-private-content" not in repr(view)
        assert "historical_input" not in repr(view)


@pytest.mark.parametrize("memory_first", [True, False])
def test_mixed_batch_preserves_real_tool_schema_results_and_order(tmp_path: Path, memory_first: bool) -> None:
    _, manager = setup(tmp_path)
    memory = ToolCall("memory", "memory_search", {"query": "private-query"})
    file_call = ToolCall("file", "read_file", {"path": "README.md"})
    calls = (memory, file_call) if memory_first else (file_call, memory)
    replies = {"memory": '{"memories":[],"omitted":0}', "file": "current file body"}
    history = [Message("assistant", "", calls), *[Message("tool", replies[call.id], call_id=call.id) for call in calls]]
    original = repr(history)
    view = manager.projection.project_messages(history)
    assert view[0].tool_calls == (file_call,) and view[1] == Message("tool", replies["file"], call_id="file")
    assert "private-query" not in repr(view) and repr(history) == original
    schemas = {item["function"]["name"]: item["function"]["parameters"]
               for item in default_tools(skill_catalog={}, memory_enabled=True).schemas()}
    for message in view:
        for call in message.tool_calls:
            spec = schemas[call.name]
            assert set(spec["required"]) <= call.arguments.keys() <= spec["properties"].keys()
            assert call.name not in MEMORY_TOOLS
    ContextManager(Settings("test", None, None, tmp_path)).validate_tool_message_groups(view)


def test_collapsed_memory_observations_still_follow_tool_output_budget(tmp_path: Path) -> None:
    context = ContextManager(Settings("test", None, None, tmp_path))
    call = ToolCall("file", "read_file", {"path": "README.md"})
    note = Message("assistant", MEMORY_OBSERVATION_HEADER + "x" * 5000, (call,))
    user = Message("user", MEMORY_OBSERVATION_HEADER + "x" * 5000)
    view = context.snip_tool_outputs([user, note, Message("tool", "file output", call_id=call.id)], 128)
    assert view[0] == user and view[1].tool_calls == (call,) and "[snipped" in view[1].content
    context.validate_tool_message_groups(view)


def test_real_agent_can_correct_bad_arguments_search_empty_store_and_save(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    calls = [ToolCall("bad", "memory_search", {"historical_input": "omitted; consult memory references"}),
             ToolCall("search", "memory_search", {"query": ""}),
             ToolCall("save", "memory_remember", {"kind": "semantic", "key": "language",
                 "content": "Use Chinese", "evidence": "以后用中文"})]
    model = ScriptedLLM([*[LLMReply("", Message("assistant", "", (call,)), (call,)) for call in calls],
                         LLMReply("done", Message("assistant", "done"))])
    class Runtime:
        def get_task_state(self):
            return ""
        def get_incomplete_tasks(self):
            return {}
        def execute_tool(self, *args):
            raise AssertionError("memory operations must use the host route")
    agent = Agent(model, default_tools(skill_catalog={}, memory_enabled=True),
                  DefaultPermissionPolicy(yes=True), Settings("test", None, None, tmp_path), Runtime(), memory=manager)
    outcome = agent.run("以后用中文")
    assert outcome.stop_reason == "completed" and len(model.requests) == 4
    assert outcome.tool_call_counts == {"memory_search": 2, "memory_remember": 1}
    assert outcome.tool_results[0].is_error and not outcome.tool_results[1].is_error
    assert json.loads(outcome.tool_results[1].content) == {"memories": [], "omitted": 0}
    assert records(model.requests[1])[0]["result"]["error"] == "Error: unexpected memory argument"
    assert records(model.requests[2])[1]["result"] == {"memories": [], "omitted": 0}
    assert "historical_input" not in repr(model.requests[1:])
    assert store.search()[0].content == "Use Chinese" and store.search()[0].status == "active"
    raw = repr(agent.history)
    agent.context.build_view(agent.history)
    assert repr(agent.history) == raw
