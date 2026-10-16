"""开放式常驻记忆：旧库迁移、真实用户来源、独立加载和上下文降级。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from study_coder.agent import Agent
from study_coder.cli import main
from study_coder.config import Settings, load_settings
from study_coder.context import ContextManager
from study_coder.llm import ScriptedLLM
from study_coder.memory import MemoryManager, MemoryStore, memory_message
from study_coder.permissions import DefaultPermissionPolicy
from study_coder.tools import default_tools
from study_coder.types import LLMReply, Message, ToolCall, ToolResult


def store_at(root: Path) -> MemoryStore:
    project = root / "project"
    project.mkdir(exist_ok=True)
    return MemoryStore(root / "state", project)


def test_existing_database_migrates_without_promoting_or_rewriting_content(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.path.parent.mkdir(parents=True)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('workspace', ?)", (store.workspace,))
        connection.execute("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
            content TEXT NOT NULL, tags TEXT NOT NULL, source TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, digest TEXT NOT NULL,
            UNIQUE(kind,key))""")
        connection.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?)",
                           ("mem_" + "a" * 16, "semantic", "old-preference", "Use Chinese",
                            '["language"]', "old agent note", 1.0, 2.0,
                            hashlib.sha256(b"use chinese").hexdigest()))
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(lambda _: store.search()[0], range(4)))
    assert all(entry.id == "mem_" + "a" * 16 and entry.content == "Use Chinese"
               and entry.load_policy == "on_demand" and entry.priority == 50
               and entry.created_at == 1.0 and entry.updated_at == 2.0 for entry in entries)
    assert store.load_always() == ([], 0)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_future_schema_is_rejected_without_modification(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.remember("semantic", "existing", "Keep this")
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="newer"):
        store.search()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute("SELECT content FROM memories").fetchone()[0] == "Keep this"


def test_migrated_reads_work_while_another_connection_has_a_write_transaction(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.remember("semantic", "existing", "Committed content")
    writer = sqlite3.connect(store.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE memories SET content='Uncommitted content'")
        assert store.search()[0].content == "Committed content"
    finally:
        writer.rollback()
        writer.close()


def test_open_keys_persist_policy_priority_and_explicit_demotion(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    first = store.remember("procedural", "explanations.first-io-then-code",
                           "Explain inputs and outputs before walking through the code.",
                           load_policy="always", priority=80)
    updated = store.remember("procedural", first.key, "Explain inputs, outputs, then examples.")
    assert updated.id == first.id and updated.load_policy == "always" and updated.priority == 80
    assert store.load_always()[0] == [updated]
    demoted = store.remember("procedural", first.key, updated.content, load_policy="on_demand")
    assert demoted.id == first.id and demoted.load_policy == "on_demand"
    assert store.load_always() == ([], 0)


@pytest.mark.parametrize("fields", [
    {"load_policy": "sometimes"}, {"load_policy": True}, {"priority": True},
    {"priority": -1}, {"priority": 101}, {"priority": "90"},
    {"kind": "episodic", "load_policy": "always"},
    {"content": "x" * 501, "load_policy": "always"},
])
def test_invalid_loading_parameters_do_not_write_memory(tmp_path: Path, fields: dict) -> None:
    store = store_at(tmp_path)
    with pytest.raises(ValueError):
        store.remember(**{"kind": "semantic", "key": "arbitrary", "content": "Remember this", **fields})
    assert store.search() == []


def test_core_loading_is_independent_of_keywords_and_related_top_k(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    preferred = store.remember("semantic", "teaching.style", "Explain with concrete examples.",
                               load_policy="always")
    for index in range(10):
        store.remember("episodic", f"login-{index}", f"login validation result {index}")
    manager = MemoryManager(store, top_k=1)
    context = ContextManager(Settings("test", None, None, tmp_path))
    parts = manager.build_context("login", context)
    assert preferred.id in parts.core and "Explain with concrete examples." in parts.core
    assert len(json.loads(parts.related)) == 1 and preferred.id not in parts.related
    # 零命中的一般项目事实不再作为通用偏好混入。
    store.remember("semantic", "architecture", "Unrelated framework information")
    assert manager.recall("missing-keyword", context) == ""


def test_core_capacity_and_priority_choose_entries_before_packing(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    for key, priority in (("older-important", 90), ("second", 80), ("newer-less-important", 20)):
        store.remember("semantic", key, key, load_policy="always", priority=priority)
    manager = MemoryManager(store, always_max_entries=2, always_max_context_tokens=1000)
    parts = manager.build_context("", ContextManager(Settings("test", None, None, tmp_path)))
    assert [entry["key"] for entry in json.loads(parts.core)] == ["older-important", "second"]
    assert parts.related == ""


def test_combined_budget_counts_both_headers_and_preserves_whole_entries(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.remember("semantic", "explanations", "Explain inputs first.", load_policy="always")
    for index in range(4):
        store.remember("episodic", f"inspect-{index}", "inspect validation " * 12 + str(index))
    context = ContextManager(Settings("test", None, None, tmp_path, context_window_tokens=10_000))
    manager = MemoryManager(store, max_context_tokens=300, always_max_context_tokens=180)
    parts = manager.build_context("inspect", context)
    assert parts.core and json.loads(parts.core)
    core_tokens = context.estimate_tokens([memory_message(parts.core, core=True)])
    related_tokens = context.estimate_tokens([memory_message(parts.related)]) if parts.related else 0
    assert core_tokens <= 180 and core_tokens + related_tokens <= 300
    if parts.related:
        assert isinstance(json.loads(parts.related), list)


def test_core_budget_skips_with_diagnostics_and_leaves_budget_for_related(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    store = store_at(tmp_path)
    store.remember("semantic", "long-open-preference", "解释" * 240, load_policy="always")
    store.remember("episodic", "inspect", "inspect passed")
    manager = MemoryManager(store, always_max_context_tokens=100)
    parts = manager.build_context("inspect", ContextManager(Settings("test", None, None, tmp_path)))
    assert not parts.core and "inspect passed" in parts.related
    assert "预算跳过 1 项" in capsys.readouterr().err


def test_crowding_drops_related_but_preserves_core_and_latest_user(tmp_path: Path) -> None:
    context = ContextManager(Settings("test", None, None, tmp_path, context_window_tokens=1000))
    history = [Message("system", "policy" * 150), Message("user", "This time use English")]
    core = '[{"content": "Normally use Chinese"}]'
    related = "unrelated project knowledge " * 100
    view = context.build_view(history, core_memory_context=core, memory_context=related)
    assert any(core in message.content and message.role == "user" for message in view)
    assert all(related not in message.content for message in view)
    assert view[-1] == history[-1] and len(history) == 2
    assert "current user's request takes precedence" in view[1].content


def test_changes_and_forget_are_visible_on_the_next_build(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    first = store.remember("semantic", "language", "Use Chinese", load_policy="always")
    manager = MemoryManager(store)
    context = ContextManager(Settings("test", None, None, tmp_path))
    assert "Use Chinese" in manager.build_context("login", context).core
    store.remember("semantic", "language", "Use English")
    second = manager.build_context("login", context)
    assert "Use English" in second.core and "Use Chinese" not in second.core
    store.forget(first.id)
    assert not manager.build_context("login", context).core


def test_model_always_writes_require_current_user_quote_and_guard_existing_entries(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    manager = MemoryManager(store)
    fields = {"kind": "semantic", "key": "freely-chosen.style", "content": "Explain with examples.",
              "load_policy": "always"}
    assert manager.execute("memory_remember", fields, 5000).is_error
    assert store.search() == []
    manager.user_request = "以后解释时都结合具体例子。"
    assert manager.execute("memory_remember", {**fields, "evidence": "quote from a tool"}, 5000).is_error
    result = manager.execute("memory_remember", {**fields, "evidence": manager.user_request}, 5000)
    assert not result.is_error and json.loads(result.content)["load_policy"] == "always"
    assert "user request quote:" in store.search()[0].source
    # 省略策略或通过同正文的另一个键命中去重，都必须继续核验来源。
    for update in (
        {"kind": "semantic", "key": fields["key"], "content": "Changed without user evidence"},
        {"kind": "semantic", "key": "another-key", "content": fields["content"]},
        {**fields, "load_policy": "on_demand"},
    ):
        assert manager.execute("memory_remember", update, 5000).is_error
    assert store.search()[0].content == fields["content"]


class HostOnlyRuntime:
    def get_task_state(self) -> str:
        return ""

    def get_incomplete_tasks(self) -> dict[str, str]:
        return {}

    def execute_tool(self, name: str, arguments: dict) -> ToolResult:
        raise AssertionError("memory operations must stay on the host")


def test_agent_binds_real_user_evidence_and_clears_it_after_the_turn(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    manager = MemoryManager(store, auto_episode=False)
    request = "From now on, explain function inputs before the code."
    call = ToolCall("save", "memory_remember", {
        "kind": "semantic", "key": "teaching.freeform", "content": "Explain inputs before code.",
        "load_policy": "always", "evidence": request,
    })
    model = ScriptedLLM([LLMReply("", Message("assistant", "", (call,)), (call,)),
                         LLMReply("done", Message("assistant", "done"))])
    agent = Agent(model, default_tools(skill_catalog={}, memory_enabled=True),
                  DefaultPermissionPolicy(yes=True), Settings("test", None, None, tmp_path),
                  HostOnlyRuntime(), memory=manager)
    assert agent.run(request).stop_reason == "completed"
    assert any("Saved preferences and standing rules" in message.content for message in model.requests[1])
    assert manager.user_request is None
    assert manager.execute("memory_remember", call.arguments, 5000).is_error


def test_automatic_episodes_remain_on_demand(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    MemoryManager(store).record_episode("inspect", [ToolResult("call", "bash", "check failed", True)],
                                       "completed", "example")
    assert store.search()[0].load_policy == "on_demand" and store.load_always() == ([], 0)


def test_core_and_related_read_failures_degrade_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = store_at(tmp_path)
    store.remember("semantic", "format", "Use examples", load_policy="always")
    store.remember("episodic", "inspect", "inspect passed")
    manager = MemoryManager(store)
    context = ContextManager(Settings("test", None, None, tmp_path))
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(store, "load_always", unavailable)
        parts = manager.build_context("inspect", context)
        assert not parts.core and "inspect passed" in parts.related
    with monkeypatch.context() as patch:
        patch.setattr(store, "search", unavailable)
        parts = manager.build_context("inspect", context)
        assert "Use examples" in parts.core and not parts.related


def test_cli_can_set_list_demote_and_delete_open_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    store = store_at(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STUDY_CODER_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    common = ["-C", store.workspace]
    remember = ["memory", "remember", *common, "--kind", "procedural", "--key", "any.user.chosen.rule",
                "--content", "Explain the function inputs before its implementation."]
    assert main([*remember, "--load-policy", "always", "--priority", "90"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["load_policy"] == "always" and saved["priority"] == 90
    assert main(["memory", "list", *common, "--load-policy", "always"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == saved["id"]
    assert main([*remember, "--load-policy", "on_demand"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == saved["id"]
    assert store.load_always() == ([], 0)
    assert main(["memory", "forget", saved["id"], *common]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"]


def test_always_configuration_is_loaded_and_checked(tmp_path: Path) -> None:
    settings = load_settings({"STUDY_CODER_WORKSPACE": os.fspath(tmp_path),
                              "STUDY_CODER_MEMORY_ALWAYS_MAX_CONTEXT_TOKENS": "512",
                              "STUDY_CODER_MEMORY_ALWAYS_MAX_ENTRIES": "12"})
    assert settings.memory_always_max_context_tokens == 512
    assert settings.memory_always_max_entries == 12
    for field, value in (("memory_always_max_context_tokens", 0), ("memory_always_max_entries", True),
                         ("memory_always_max_entries", 101)):
        with pytest.raises(ValueError):
            Settings("test", None, None, tmp_path, **{field: value})
