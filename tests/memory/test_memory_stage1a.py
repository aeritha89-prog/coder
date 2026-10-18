"""第一阶段无迁移修复：预算准入、原值保护、返回语义和摘要权限。"""

import json
import sqlite3
from pathlib import Path

import pytest

from study_coder.cli import main
from study_coder.config import Settings
from study_coder.conversation.context import ContextManager
from study_coder.memory.budget import core_tokens
from study_coder.memory.core import MemoryManager, MemoryStore
from study_coder.types import Message


def setup_memory(root: Path) -> tuple[MemoryStore, MemoryManager]:
    return (store := MemoryStore(root / "state", root)), MemoryManager(store)


def test_new_oversize_preference_reports_actual_policy_and_valid_compact_result(tmp_path: Path) -> None:
    store, manager = setup_memory(tmp_path)
    manager.user_request = "以后都这样解释"
    fields = {"kind": "semantic", "key": "explanation", "content": "中" * 300,
              "load_policy": "always", "evidence": manager.user_request}
    assert core_tokens("mem_" + "a" * 16, fields["key"], fields["content"]) > 256
    result = manager.execute("memory_remember", fields, 160)
    data = json.loads(result.content)
    assert not result.is_error and len(result.content) <= 160
    assert data["saved"] and data["load_policy"] == "on_demand"
    assert data["activated_as_always"] is False and data["reason"] == "core_budget_exceeded"
    assert store.search()[0].load_policy == "on_demand"


def test_failed_always_update_preserves_content_and_database_shape(tmp_path: Path) -> None:
    store, manager = setup_memory(tmp_path)
    first = manager.remember("semantic", "language", "Use Chinese", load_policy="always")
    with sqlite3.connect(store.path) as connection:
        before = (connection.execute("PRAGMA user_version").fetchone(),
                  connection.execute("PRAGMA table_info(memories)").fetchall())
    with pytest.raises(ValueError, match="not changed"):
        manager.remember("semantic", "language", "中" * 300)
    assert store.search()[0].id == first["id"] and store.search()[0].content == "Use Chinese"
    with sqlite3.connect(store.path) as connection:
        assert before == (connection.execute("PRAGMA user_version").fetchone(),
                          connection.execute("PRAGMA table_info(memories)").fetchall())


def test_budget_demotion_cannot_bypass_requested_always_evidence(tmp_path: Path) -> None:
    store, manager = setup_memory(tmp_path)
    result = manager.execute("memory_remember", {"kind": "semantic", "key": "long",
        "content": "中" * 300, "load_policy": "always"}, 5000)
    assert result.is_error and store.search() == []
    assert json.loads(manager.execute("memory_forget", {"id": "mem_" + "a" * 16}, 160).content) == {
        "deleted": False}


def test_cli_uses_same_budget_as_model_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STUDY_CODER_SANDBOX_STATE_DIR", str(tmp_path / "state"))
    assert main(["memory", "remember", "-C", str(tmp_path), "--kind", "semantic", "--key", "long",
                 "--content", "中" * 300, "--load-policy", "always"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["load_policy"] == "on_demand" and result["reason"] == "core_budget_exceeded"


def test_cached_summary_is_data_and_does_not_mutate_raw_coverage(tmp_path: Path) -> None:
    context = ContextManager(Settings("test", None, None, tmp_path))
    history = [Message("system", "policy"), Message("user", "old"),
               Message("assistant", "old reply"), Message("user", "This time use English")]
    context._summary_keys = context._group_keys([(history[1],), (history[2],)])
    context._summary_text = "Earlier preference was Chinese"
    keys = context._summary_keys
    view = context.build_view(history)
    assert any(message.role == "user" and "Earlier conversation summary" in message.content
               for message in view)
    assert view[-1] == history[-1] and context._summary_keys == keys
    assert all("Earlier preference" not in message.content for message in history)
