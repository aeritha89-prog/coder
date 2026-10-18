"""活动技能的消息顺序、预算、摘要与会话恢复回归测试。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from study_coder.agent import Agent
from study_coder.config import Settings, load_settings
from study_coder.conversation.context import ContextError, ContextManager
from study_coder.conversation.session import load_session_bundle, save_session
from study_coder.llm import ScriptedLLM
from study_coder.permissions import DefaultPermissionPolicy
from study_coder.tools import default_tools
from study_coder.tools.base import ToolFailure
from study_coder.tools.skillload import skill_loader
from study_coder.types import LLMReply, Message, ToolCall, ToolResult


class Runtime:
    def __init__(self, catalog: dict[str, dict[str, str]], failure: bool = False) -> None:
        self.catalog = catalog
        self.failure = failure
        self.override: str | None = None

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "skill_loader":
            body = self.catalog[arguments["skill_name"]]["body"]
            return ToolResult("", name, self.override if self.override is not None else body, self.failure)
        return ToolResult("", name, "file content")

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_incomplete_tasks(self) -> dict[str, str]:
        return {}

    def get_task_state(self) -> str:
        return ""


def make_agent(root: Path, replies: list[LLMReply], body: str = "FULL SKILL INSTRUCTIONS",
               **budgets: int) -> tuple[Agent, ScriptedLLM, Runtime]:
    catalog = {"test": {"description": "Test skill", "body": body}}
    settings = Settings("scripted", None, None, root, **budgets)
    runtime = Runtime(catalog)
    model = ScriptedLLM(replies)
    agent = Agent(model, default_tools(skill_catalog=catalog), DefaultPermissionPolicy(),
                  settings, runtime)
    return agent, model, runtime


def reply(*calls: ToolCall) -> LLMReply:
    return LLMReply("", Message("assistant", "", calls), calls)


def done() -> LLMReply:
    return LLMReply("done", Message("assistant", "done"))


def skill(number: str = "s") -> ToolCall:
    return ToolCall(number, "skill_loader", {"skill_name": "test"})


def test_multi_call_order_and_checkpoint_have_no_skill_system(tmp_path: Path) -> None:
    agent, model, _ = make_agent(tmp_path, [
        reply(skill(), ToolCall("read", "read_file", {"path": "a.txt"})), done()])
    checkpoints = []
    agent.history_checkpoint = lambda history: checkpoints.append((list(history), agent.skill_state()))
    result = agent.run("load and read")
    assert result.stop_reason == "completed"
    call_index = next(i for i, m in enumerate(agent.history) if m.tool_calls)
    assert [m.role for m in agent.history[call_index:call_index + 3]] == ["assistant", "tool", "tool"]
    assert all(m.role != "system" for m in agent.history[call_index + 1:])
    assert len(checkpoints) == 1 and checkpoints[0][1] == agent.skill_state()
    agent.context.validate_tool_message_groups(checkpoints[0][0])
    ContextManager(agent.settings).validate_tool_message_groups(model.requests[1])
    assert sum(m.content.count("FULL SKILL INSTRUCTIONS") for m in model.requests[1]) == 1
    assert all("FULL SKILL INSTRUCTIONS" not in m.content for m in agent.history)


def test_repeat_load_is_deduplicated_and_clear_resets_skills(tmp_path: Path) -> None:
    agent, model, _ = make_agent(tmp_path, [reply(skill()), reply(skill("s2")), done()])
    result = agent.run("load twice")
    assert result.stop_reason == "completed"
    assert "already active" in result.tool_results[1].content
    assert list(agent.active_skills) == ["test"]
    assert sum(m.content.count("FULL SKILL INSTRUCTIONS") for m in model.requests[2]) == 1
    agent.clear()
    assert agent.active_skills == {} and len(agent.history) == 1


@pytest.mark.parametrize("mode", ["failure", "mismatch", "single_limit", "total_limit", "window"])
def test_failed_or_oversized_skill_never_activates(tmp_path: Path, mode: str) -> None:
    budgets = {"single_limit": {"max_skill_chars": 5},
               "total_limit": {"max_active_skill_chars": 5},
               "window": {"context_window_tokens": 10_000}}.get(mode, {})
    body = "汉" * 6_000 if mode == "window" else "FULL SKILL INSTRUCTIONS"
    agent, model, runtime = make_agent(tmp_path, [reply(skill()), done()], body, **budgets)
    if mode == "failure":
        runtime.failure = True
    if mode == "mismatch":
        runtime.override = "truncated skill"
    result = agent.run("load")
    assert result.stop_reason == "completed"
    assert result.tool_results[0].is_error and agent.active_skills == {}
    assert not any(m.content.startswith("Active skills:") for m in model.requests[1])


def test_skill_is_complete_even_when_general_tool_output_limit_is_tiny(tmp_path: Path) -> None:
    body = "instruction " * 1_000
    agent, model, _ = make_agent(tmp_path, [reply(skill()), done()], body,
                                 max_tool_output_chars=20)
    assert agent.run("load").stop_reason == "completed"
    assert agent.active_skills["test"] == body
    assert any(body in m.content for m in model.requests[1])


def test_skill_survives_history_compression_and_counts_schema_budget(tmp_path: Path) -> None:
    settings = Settings("scripted", None, None, tmp_path, context_window_tokens=1_000)
    manager = ContextManager(settings)
    history = [Message("system", "Policy"), Message("user", "Task")]
    for i in range(10):
        c = ToolCall(str(i), "read_file", {"path": "file"})
        history += [Message("assistant", "", (c,)), Message("tool", "x" * 800, call_id=c.id)]
    view = manager.build_view(history, active_skills={"test": "完整技能" * 10},
                              tool_schemas=[{"description": "small schema"}])
    assert len(view) < len(history)
    assert sum(m.content.count("完整技能" * 10) for m in view) == 1
    assert history[0].content == "Policy"
    manager.validate_tool_message_groups(view)
    with pytest.raises(ContextError, match="tool schemas"):
        manager.build_view(history[:2], tool_schemas=[{"description": "x" * 5_000}])


def test_session_roundtrip_restores_only_current_trusted_body(tmp_path: Path) -> None:
    agent, _, _ = make_agent(tmp_path, [reply(skill()), done()])
    agent.run("load")
    path = save_session(tmp_path, "test", agent.settings, agent.history, agent.skill_state())
    assert "FULL SKILL INSTRUCTIONS" not in path.read_text(encoding="utf-8")
    history, skills, _ = load_session_bundle(tmp_path, "test", agent.settings)
    restored, model, _ = make_agent(tmp_path, [done()])
    restored.restore_session(history, skills)
    assert restored.history == history and restored.history is not history
    assert restored.active_skills == agent.active_skills
    assert restored.run("continue").stop_reason == "completed"
    assert any("FULL SKILL INSTRUCTIONS" in m.content for m in model.requests[0])
    changed, _, _ = make_agent(tmp_path, [], body="changed")
    with pytest.raises(ToolFailure, match="changed"):
        changed.restore_session(history, skills)
    changed.tools = default_tools(skill_catalog={})
    with pytest.raises(ToolFailure, match="unavailable"):
        changed.restore_session(history, skills)


def test_restore_session_rejects_interrupted_tool_result_group(tmp_path: Path) -> None:
    agent, _, _ = make_agent(tmp_path, [])
    initial_history = list(agent.history)
    calls = (skill(), ToolCall("read", "read_file", {"path": "file"}))
    history = [Message("system", "Policy"), Message("assistant", "", calls),
               Message("tool", "Activated skill 'test'.", call_id="s"),
               Message("system", "Unexpected message between tool results"),
               Message("tool", "file", call_id="read")]
    with pytest.raises(ContextError, match="lack matching results"):
        agent.restore_session(history, {})
    assert agent.history == initial_history
    assert agent.active_skills == {}


def test_budget_env_and_invalid_values(tmp_path: Path) -> None:
    settings = load_settings({"STUDY_CODER_WORKSPACE": str(tmp_path),
                              "STUDY_CODER_MAX_SKILL_CHARS": "1234",
                              "STUDY_CODER_MAX_ACTIVE_SKILL_CHARS": "2345"})
    assert settings.max_skill_chars == 1234 and settings.max_active_skill_chars == 2345
    for bad in (0, -1, True, 1_000_001):
        with pytest.raises(ValueError):
            replace(settings, max_skill_chars=bad)


def test_catalog_is_image_authoritative_and_invalid_catalog_is_rejected(tmp_path: Path) -> None:
    catalog = {"image-only": {"description": "In image", "body": "From image"}}
    tools = default_tools(skill_catalog=catalog)
    loader = tools.get("skill_loader")
    assert isinstance(loader, skill_loader) and set(loader.skills) == {"image-only"}
    with pytest.raises(ToolFailure):
        skill_loader.from_catalog({"bad": {"body": "missing description"}})
