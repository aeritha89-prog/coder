"""当前记忆生命周期：事务建库/并发，以及原始历史不变的模型视图。"""

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
from study_coder.context import ContextManager, ContextSummary
from study_coder.memory import MemoryManager, MemoryStore
from study_coder.memory_schema import fingerprint, initialize_schema, record_change
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
    import study_coder.memory_schema as schema
    monkeypatch.setattr(schema, "MAX_EVENTS", 5)
    store, _ = setup(tmp_path)
    for index in range(25):
        entry = store.remember("semantic", "choice", f"choice {index}")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 20
        assert connection.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] == 5
    assert store.get(entry.id, 1) is None and store.get(entry.id, 6) is not None


def test_forget_wipes_versions_and_blocks_old_facts_until_user_reintroduces_them(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    first = store.remember("semantic", "language", "Chinese")
    store.remember("semantic", "language", "English")
    assert store.forget(first.id) and store.get(first.id, 1) is None
    with sqlite3.connect(store.path) as connection:
        row = connection.execute("SELECT key,content,recall_text,source,source_refs FROM memories").fetchone()
        assert row == ("\0" + first.id, "", "", "", "[]")
        assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 0
    with pytest.raises(ValueError, match="withdrawn"):
        store.remember("semantic", "new-key", "Chinese", allow_recreate=False)
    manager.user_request = "以后继续用中文"
    result = manager.execute("memory_remember", {"kind": "semantic", "key": "language", "content": "Chinese",
                                                "evidence": manager.user_request}, 5000)
    assert not result.is_error and json.loads(result.content)["id"] != first.id
    assert store.get(first.id) is None and not store.forget(first.id)


def test_same_source_episode_is_withdrawn_but_new_independent_observation_is_allowed(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    manager.auto_episode = True
    source = "task=stable;history_start=1;history_end=4;"
    manager.record_episode("install", [ToolResult("call-a", "bash", "download failed", True)], "completed", source + "a" * 32)
    first = store.search()[0]
    assert first.source_kind == "tool_observed" and first.verification_status == "observed"
    assert store.forget(first.id)
    with pytest.raises(ValueError, match="withdrawn"):
        manager.record_episode("install", [ToolResult("call-a", "bash", "download failed", True)], "completed", source + "b" * 32)
    manager.record_episode("install", [ToolResult("call-b", "bash", "download failed", True)], "completed", source + "c" * 32)
    second = store.search()[0]
    assert second.id != first.id and second.content_fingerprint == first.content_fingerprint
    assert fingerprint("procedural", "Do NOT install") != fingerprint("procedural", "Do install")


def test_withdrawal_covers_sources_older_than_retained_versions(tmp_path: Path) -> None:
    store, _ = setup(tmp_path)
    for index in range(25):
        entry = store.remember("episodic", "event", f"observation {index}",
                               source_refs=[{"kind": "tool_observation", "event_id": f"event-{index}"}])
    assert store.get(entry.id, 1) is None
    assert store.forget(entry.id)
    with pytest.raises(ValueError, match="withdrawn"):
        store.remember("episodic", "regenerated-key", "paraphrased old observation", allow_recreate=False,
                       source_refs=[{"kind": "tool_observation", "event_id": "event-0"}])
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_origins").fetchone()[0] == 0


def test_marker_capacity_failure_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import study_coder.memory as memory_module
    store, _ = setup(tmp_path)
    entry = store.remember("semantic", "preference", "Please keep this")
    revision = store.revision()
    monkeypatch.setattr(memory_module, "MAX_TOMBSTONES", 1)
    with pytest.raises(ValueError, match="capacity"):
        store.forget(entry.id)
    assert store.get(entry.id).content == entry.content and store.revision() == revision
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_tombstones").fetchone()[0] == 0


def tool_group(name: str, fields: dict, entry: dict) -> list[Message]:
    call = ToolCall("memory-call", name, fields)
    return [Message("assistant", "historical preference", (call,)),
            Message("tool", json.dumps(entry), call_id=call.id)]


def test_projection_refreshes_memory_and_forget_masks_arguments_without_changing_history(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    entry = store.remember("semantic", "language", "Chinese")
    group = tool_group("memory_remember", {"kind": "semantic", "key": "language", "content": "Chinese",
                                          "evidence": "Use Chinese forever"}, asdict(entry))
    original = repr(group)
    store.remember("semantic", "language", "English")
    view = manager.projection.project_messages(group)
    assert "English" in repr(view) and "Chinese" not in repr(view)
    ContextManager(Settings("test", None, None, tmp_path)).validate_tool_message_groups(view)
    assert store.forget(entry.id)
    assert "Chinese" not in repr(manager.projection.project_messages(group))
    assert "unavailable" in repr(manager.projection.project_messages(group))
    assert repr(group) == original
    ordinary = tool_group("read_file", {"path": "untrusted.json"}, asdict(entry))
    assert manager.projection.project_messages(ordinary) == ordinary


def test_projection_retains_actionable_revision_errors_and_compact_episode_binding(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    entry = store.remember("episodic", "build", "old failure")
    call = ToolCall("call", "memory_remember", {"key": "build", "content": "changed"})
    error = Message("tool", "Error: updating memory requires expected_revision; read its current version first", call_id=call.id)
    projected = manager.projection.project_messages([Message("assistant", "", (call,)), error])
    result = json.loads(projected[0].content.split("\n", 1)[1])[0]["result"]
    assert result["state"] == "error" and result["error"] == error.content
    compact = tool_group("memory_read", {"id": entry.id}, {"id": entry.id, "revision": 1, "truncated": True})
    store.remember("episodic", "build", "new success")
    view = manager.projection.project_messages(compact)
    assert "old failure" in repr(view) and "new success" not in repr(view)


def test_structured_summary_references_update_and_restore_without_resummarizing(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
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
            return ContextSummary("Prior operations recorded; consult memory references.")
    summary = Summary()
    context = ContextManager(Settings("test", None, None, tmp_path, context_window_tokens=2000), summary)
    context.memory_projector = manager.projection
    context.build_view(history)
    assert len(summary.calls) == 1
    assert "Chinese" not in repr(summary.calls) and "memory_references" in repr(summary.calls)
    state = context.memory_state()
    assert len(state["short_term"]["references"]) == 2 and "Chinese" not in json.dumps(state)
    keys = context.short_term_memory.covered_keys
    store.remember("semantic", "language", "English")
    store.remember("episodic", "build", "build now succeeded")
    rendered = repr(context.build_view(history))
    assert "English" in rendered and "Chinese" not in rendered
    assert "build previously failed" in rendered and "build now succeeded" not in rendered
    assert context.short_term_memory.covered_keys == keys and len(summary.calls) == 1
    store.forget(preference.id)
    restored = ContextManager(context.settings, summary)
    restored.memory_projector = manager.projection
    restored.restore_memory(state, history)
    rendered = repr(restored.build_view(history))
    assert "Chinese" not in rendered and "English" not in rendered and "unavailable" in rendered
    assert restored.short_term_memory.covered_keys == keys and len(summary.calls) == 1
    assert repr(history) == original


def test_reference_expansion_is_budgeted_and_missing_db_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, manager = setup(tmp_path)
    entry = store.remember("semantic", "long", "中" * 500)
    refs = [{"id": entry.id, "revision": 1, "binding": "current"}]
    assert manager.projection.resolve_references(refs, 20) == ""
    def unavailable(*args):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(store, "get", unavailable)
    group = tool_group("memory_read", {"id": entry.id}, {"memory": asdict(entry)})
    view = manager.projection.project_messages(group)
    assert "unavailable" in repr(view) and "中" not in repr(view)


@pytest.mark.parametrize("version,include_refs", [(1, True), (2, False)])
def test_old_or_incomplete_summary_state_is_not_restored(tmp_path: Path, version: int, include_refs: bool) -> None:
    context = ContextManager(Settings("test", None, None, tmp_path))
    history = [Message("system", "policy"), Message("user", "old request"),
               Message("assistant", "old reply"), Message("user", "new request")]
    short = {"summary": "old cached summary", "covered_keys": list(context._group_keys([(history[1],), (history[2],)]))}
    if include_refs:
        short["references"] = []
    context.restore_memory({"version": version, "short_term": short}, history)
    assert context.short_term_memory.summary == ""
    assert context.build_view(history) == history


def test_old_memory_tool_result_without_revision_is_not_guessed(tmp_path: Path) -> None:
    store, manager = setup(tmp_path)
    entry = store.remember("semantic", "language", "Chinese")
    fields = asdict(entry)
    fields.pop("revision")
    group = tool_group("memory_remember", {"content": "Chinese"}, fields)
    assert manager.projection.references_from(group) == []
    assert "Chinese" not in repr(manager.projection.project_messages(group))
    compact = manager.execute("memory_remember", {"kind": "semantic", "key": "candidate", "content": "new inference"}, 160)
    assert not compact.is_error and json.loads(compact.content)["revision"] == 1


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
