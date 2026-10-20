"""记忆事务、版本和状态，以及普通会话历史与摘要恢复。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from study_coder.cli import main
from study_coder.config import Settings
from study_coder.conversation.context import ContextManager, ContextSummary
from study_coder.memory.core import MemoryManager, MemoryStore
from study_coder.memory.schema import fingerprint, initialize_schema, record_change
from study_coder.types import Message, ToolCall, ToolResult


def setup(root: Path, maximum: int = 1000) -> tuple[MemoryStore, MemoryManager]:
    store = MemoryStore(root / "state", root, maximum)
    return store, MemoryManager(store, auto_episode=False)


def fill_current_store(store: MemoryStore, kinds: list[str]) -> None:
    """用一个事务准备满容量当前库，保留真实版本和来源记录。"""
    connection = store._connect()
    try:
        with connection:
            for index, kind in enumerate(kinds):
                identifier, content = f"mem_{index:016x}", f"current fact {index}"
                connection.execute("""INSERT INTO memories
                    (id,kind,key,content,tags,source,created_at,updated_at,digest,recall_text,scope_id,content_fingerprint)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (identifier, kind, f"fact-{index}", content, '[]', "user action", 1.0, 2.0,
                     hashlib.sha256(content.encode()).hexdigest(), content, store.workspace, fingerprint(kind, content)))
                record_change(connection, identifier, "remember")
    finally:
        connection.close()


@pytest.mark.parametrize("kinds", [["episodic"] * 1000, ["semantic"] * 1000,
                                  ["semantic", "procedural", "episodic"] * 333 + ["procedural"]])
def test_full_current_database_versions_do_not_consume_quota(tmp_path: Path, kinds: list[str]) -> None:
    store, _ = setup(tmp_path)
    fill_current_store(store, kinds)
    first = store.search(limit=100)[0]
    assert first.status == "active" and first.source_kind == "user_action"
    assert first.verification_status == "unknown" and first.verified_at is None
    assert (first.created_at, first.updated_at) == (1.0, 2.0)
    assert first.recall_text == first.content and first.content_fingerprint
    updated = store.remember(first.kind, first.key, "explicit replacement", expected_revision=1)
    assert updated.id == first.id and updated.revision == 2 and updated.supersedes_revision == 1
    assert store.get(first.id, 1).status == "superseded"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1000
        assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 1001
        indices = {row[1] for row in connection.execute("PRAGMA index_list(memories)")}
        assert {"memories_loading", "memories_digest", "memories_state"} <= indices


def test_interrupted_initialization_rolls_back_and_can_retry(tmp_path: Path) -> None:
    store, _ = setup(tmp_path)
    store.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    # 在建表之后拒绝索引 DDL，验证初始 schema 不会留下半份状态。
    connection.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                              if action == sqlite3.SQLITE_CREATE_INDEX else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            initialize_schema(connection, store.workspace)
    finally:
        connection.close()
    with sqlite3.connect(store.path) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0] == 0
    assert store.remember("semantic", "retry", "Initialization retry succeeded").status == "active"


def test_only_one_concurrent_expected_revision_update_can_commit(tmp_path: Path) -> None:
    store, _ = setup(tmp_path)
    entry = store.remember("semantic", "language", "Chinese")
    def update(value: str) -> str:
        try:
            store.remember("semantic", "language", value, expected_revision=1)
            return "committed"
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["English", "French"]))
    assert results.count("committed") == 1
    assert sum("revision conflict" in result for result in results) == 1
    assert store.get(entry.id).revision == 2 and store.revision() == 2
    assert store.get(entry.id, 1).content == "Chinese"


def test_candidate_sources_and_explicit_activation(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    fields = {"kind": "procedural", "key": "tests", "content": "Run pytest before delivery"}
    result = manager.execute("memory_remember", fields, 5000)
    value = json.loads(result.content)
    assert not result.is_error and value["status"] == "candidate"
    assert value["source_kind"] == "model_inferred" and value["source_refs"]
    context = ContextManager(Settings("test", None, None, tmp_path))
    assert manager.build_context("pytest delivery", context).related == ""
    assert store.search(status="candidate")[0].id == value["id"]
    assert json.loads(manager.execute("memory_read", {"id": value["id"]}, 5000).content)["memory"]["content"] == fields["content"]
    entry = store.set_status(value["id"], "active", 1)
    assert entry.revision == 2 and entry.verification_status == "unknown"
    assert "pytest" in manager.build_context("pytest", context).related
    # 新推断即使知道当前版本，也不能使已有有效知识静默降为候选。
    assert manager.execute("memory_remember", {**fields, "content": "Skip tests", "expected_revision": 2}, 5000).is_error
    assert store.get(entry.id).status == "active" and store.get(entry.id).revision == 2
    manager.user_request = "以后交付之前运行完整测试"
    confirmed = manager.execute("memory_remember", {**fields, "content": "Run full tests", "expected_revision": 2,
                                                   "evidence": manager.user_request}, 5000)
    assert not confirmed.is_error and json.loads(confirmed.content)["revision"] == 3
    assert store.get(entry.id).source_kind == "user_statement"
    assert manager.execute("memory_remember", {**fields, "status": "active"}, 5000).is_error


def test_full_content_and_short_recall_are_separate_and_budgeted(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    full = "When offline, do not install dependencies. " * 20
    short = "When offline, do not install dependencies."
    value = manager.remember("procedural", "offline", full, recall_text=short, load_policy="always")
    assert value["activated_as_always"] and store.get(value["id"]).content == full.strip()
    core = manager.build_context("unrelated", ContextManager(Settings("test", None, None, tmp_path))).core
    assert short in core and full not in core
    result = manager.execute("memory_read", {"id": value["id"]}, 160)
    assert not result.is_error and len(result.content) <= 160 and json.loads(result.content)["truncated"]


def test_version_and_event_retention_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import study_coder.memory.schema as schema
    monkeypatch.setattr(schema, "MAX_EVENTS", 5)
    store, _ = setup(tmp_path)
    for index in range(25):
        entry = store.remember("semantic", "choice", f"choice {index}")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 20
        assert connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] == 5
    assert store.get(entry.id, 1) is None and store.get(entry.id, 6) is not None


@pytest.mark.parametrize("with_evidence", [False, True])
def test_forget_wipes_versions_and_allows_new_memory_identity(tmp_path: Path, with_evidence: bool) -> None:
    store, manager = setup(tmp_path)
    first = store.remember("semantic", "language", "Chinese")
    store.remember("semantic", "language", "English")
    assert store.forget(first.id) and store.get(first.id, 1) is None
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT key,content,recall_text,source,source_refs FROM memories").fetchone()
        assert row == ("\0" + first.id, "", "", "", "[]")
        assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 0
    manager.user_request = "以后继续用中文"
    fields = {"kind": "semantic", "key": "language", "content": "Chinese"}
    if with_evidence:
        fields["evidence"] = manager.user_request
    result = manager.execute("memory_remember", fields, 5000)
    value = json.loads(result.content)
    assert not result.is_error and value["id"] != first.id and value["revision"] == 1
    assert value["status"] == ("active" if with_evidence else "candidate")
    assert store.get(value["id"]).content == "Chinese"
    assert store.get(first.id) is None and not store.forget(first.id)


def test_deleted_episode_can_be_saved_again_from_same_or_new_observation(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    manager.auto_episode = True
    source = "task=stable;history_start=1;history_end=4;"
    manager.record_episode("install", [ToolResult("call-a", "bash", "download failed", True)], "completed", source + "a" * 32)
    first = store.search()[0]
    assert first.source_kind == "tool_observed" and first.verification_status == "observed"
    assert store.forget(first.id)
    manager.record_episode("install", [ToolResult("call-a", "bash", "download failed", True)], "completed", source + "b" * 32)
    replayed = store.search()[0]
    assert replayed.id != first.id and replayed.content_fingerprint == first.content_fingerprint
    manager.record_episode("install", [ToolResult("call-b", "bash", "download failed", True)], "completed", source + "c" * 32)
    second = store.search()[0]
    assert second.id not in {first.id, replayed.id} and second.content_fingerprint == first.content_fingerprint
    assert store.get(first.id) is None
    assert fingerprint("procedural", "Do NOT install") != fingerprint("procedural", "Do install")


def test_forget_failure_preserves_body_versions_and_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _ = setup(tmp_path)
    entry = store.remember("semantic", "preference", "Please keep this")
    revision = store.revision()
    connect = store._connect
    def deny_body_update() -> sqlite3.Connection:
        connection = connect()
        connection.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                                  if action == sqlite3.SQLITE_UPDATE and table == "memories"
                                  else sqlite3.SQLITE_OK)
        return connection
    monkeypatch.setattr(store, "_connect", deny_body_update)
    with pytest.raises(sqlite3.DatabaseError):
        store.forget(entry.id)
    assert store.get(entry.id).content == entry.content and store.revision() == revision
    assert store.get(entry.id, entry.revision) == entry
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 1


def tool_group(name: str, fields: dict, entry: dict) -> list[Message]:
    call = ToolCall("memory-call", name, fields)
    return [Message("assistant", "historical preference", (call,)),
            Message("tool", json.dumps(entry), call_id=call.id)]


def test_memory_changes_do_not_rewrite_past_tool_results(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    entry = store.remember("semantic", "language", "Chinese")
    history = [Message("system", "policy"), Message("user", "old request")]
    history += tool_group("memory_remember", {"kind": "semantic", "key": "language", "content": "Chinese",
                                              "evidence": "Use Chinese forever"}, asdict(entry))
    history.append(Message("user", "continue"))
    original = repr(history)
    context = ContextManager(Settings("test", None, None, tmp_path))
    store.remember("semantic", "language", "English")
    assert "English" in manager.build_context("English", context).related
    assert context.build_view(history) == history
    assert store.forget(entry.id)
    assert manager.build_context("English", context).related == ""
    assert context.build_view(history) == history and repr(history) == original
    context.validate_tool_message_groups(history)


def test_plain_summary_restores_without_following_memory_changes(tmp_path: Path) -> None:
    store, _ = setup(tmp_path)
    preference = store.remember("semantic", "language", "Chinese")
    event = store.remember("episodic", "build", "build previously failed")
    history = [Message("system", "policy"), Message("user", "old request")]
    history += tool_group("memory_remember", {"content": "Chinese"}, asdict(preference))
    history += [Message("assistant", "", (ToolCall("episode", "memory_read", {"id": event.id, "revision": 1}),)),
                Message("tool", json.dumps({"memory": asdict(event)}), call_id="episode")]
    for index in range(6):
        history += [Message("assistant", "", (ToolCall(str(index), "read_file", {"path": "file"}),)),
                    Message("tool", "x" * 4000, call_id=str(index))]
    history.append(Message("user", "continue"))
    original = repr(history)
    class Summary:
        calls: list[list[Message]] = []
        def summarize(self, messages, previous=""):
            self.calls.append(list(messages))
            return ContextSummary("Historical preference: Chinese; build previously failed.")
    summary = Summary()
    context = ContextManager(Settings("test", None, None, tmp_path, context_window_tokens=2000), summary)
    context.build_view(history)
    assert len(summary.calls) == 1
    assert "Chinese" in repr(summary.calls) and "memory_remember" in repr(summary.calls)
    state = context.memory_state()
    assert state["version"] == 3
    assert set(state["short_term"]) == {"summary", "covered_keys"}
    keys = context.short_term_memory.covered_keys
    store.remember("semantic", "language", "English")
    store.remember("episodic", "build", "build now succeeded")
    rendered = repr(context.build_view(history))
    assert "Chinese" in rendered and "English" not in rendered
    assert "build previously failed" in rendered and "build now succeeded" not in rendered
    assert context.short_term_memory.covered_keys == keys and len(summary.calls) == 1
    store.forget(preference.id)
    restored = ContextManager(context.settings, summary)
    restored.restore_memory(state, history)
    rendered = repr(restored.build_view(history))
    assert "Chinese" in rendered and "English" not in rendered
    assert store.get(preference.id) is None
    assert restored.short_term_memory.covered_keys == keys and len(summary.calls) == 1
    assert repr(history) == original


@pytest.mark.parametrize("version,complete", [(1, True), (2, True), (3, False)])
def test_old_or_incomplete_summary_state_is_not_restored(tmp_path: Path, version: int, complete: bool) -> None:
    context = ContextManager(Settings("test", None, None, tmp_path))
    history = [Message("system", "policy"), Message("user", "old request"),
               Message("assistant", "old reply"), Message("user", "new request")]
    short = {"summary": "old cached summary"}
    if complete:
        short["covered_keys"] = list(context._group_keys([(history[1],), (history[2],)]))
    context.restore_memory({"version": version, "short_term": short}, history)
    assert context.short_term_memory.summary == ""
    assert context.build_view(history) == history


def test_cli_status_and_historical_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STUDY_CODER_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    store, _ = setup(tmp_path)
    first = store.remember("procedural", "tests", "Run pytest", status="candidate", source_kind="model_inferred")
    common = ["-C", str(tmp_path)]
    assert main(["memory", "approve", first.id, *common, "--expected-revision", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "active"
    assert main(["memory", "read", first.id, *common, "--revision", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "superseded"
    assert main(["memory", "archive", first.id, *common, "--expected-revision", "1"]) == 2
    assert "revision conflict" in capsys.readouterr().err
    assert main(["memory", "archive", first.id, *common, "--expected-revision", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "archived"
    assert store.search(recall=True) == []
