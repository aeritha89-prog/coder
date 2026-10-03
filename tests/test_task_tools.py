"""任务参数、磁盘记录和有界上下文的回归测试。"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

import study_coder.tools.task as task_module
from study_coder.config import Settings
from study_coder.tools.base import ToolContext, ToolFailure


@pytest.fixture
def task_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolContext:
    monkeypatch.setattr(task_module, "WORKDIR", tmp_path)
    monkeypatch.setattr(task_module, "TASKS", task_module.TaskStore(tmp_path / ".tasks"))
    return ToolContext(tmp_path, tmp_path, Settings("test", None, None, tmp_path))


@pytest.mark.parametrize("tool_class", [
    task_module.get_task, task_module.claim_task, task_module.complete_task,
    task_module.update_task_dependencies, task_module.delete_task,
])
@pytest.mark.parametrize("task_id", [None, 123, "../task_12345678", "task_bad"])
def test_task_id_errors_are_failure_observations(task_context: ToolContext, tool_class, task_id) -> None:
    arguments = {} if task_id is None else {"task_id": task_id}
    result = tool_class().execute(arguments, task_context)
    assert result.is_error and "task_id" in result.content
    json.dumps(asdict(result))


@pytest.mark.parametrize("arguments", [
    {}, {"subject": None}, {"subject": []}, {"subject": "  "},
    {"subject": "valid", "description": None},
])
def test_create_rejects_invalid_text_before_writing(task_context: ToolContext, arguments: dict) -> None:
    assert task_module.create_task().execute(arguments, task_context).is_error
    assert task_module.TASKS.list() == []


@pytest.mark.parametrize("dependencies", [None, "task_12345678", [None], [{}], [False]])
def test_invalid_dependencies_leave_existing_plan_unchanged(
    task_context: ToolContext, dependencies,
) -> None:
    store = task_module.TASKS
    parent = store.create("parent")
    child = store.create("child")
    store.update_dependencies(child.id, [parent.id])
    result = task_module.update_task_dependencies().execute(
        {"task_id": child.id, "add_blocked_by": dependencies}, task_context)
    assert result.is_error
    assert store.load(child.id).blockedBy == [parent.id]


@pytest.mark.parametrize("field, value", [
    ("subject", None), ("description", []), ("status", []), ("owner", False),
    ("blockedBy", None), ("blockedBy", [{}]), ("id", "task_00000000"),
])
def test_task_file_field_types_are_validated(
    task_context: ToolContext, field: str, value,
) -> None:
    task = task_module.TASKS.create("valid")
    payload = asdict(task)
    payload[field] = value
    task_module.TASKS._path(task.id).write_text(json.dumps(payload), encoding="utf-8")
    result = task_module.get_task().execute({"task_id": task.id}, task_context)
    assert result.is_error and task.id in result.content


@pytest.mark.parametrize("payload", [[], None, {"id": "task_12345678"}, {"unexpected": True}])
def test_task_file_structure_errors_are_explained(task_context: ToolContext, payload) -> None:
    task = task_module.TASKS.create("valid")
    task_module.TASKS._path(task.id).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ToolFailure, match="Invalid task file"):
        task_module.TASKS.load(task.id)


def test_replacing_dependencies_and_cycle_rejection(task_context: ToolContext) -> None:
    store = task_module.TASKS
    first = store.create("first")
    second = store.create("second")
    store.update_dependencies(second.id, [first.id, first.id])
    assert store.load(second.id).blockedBy == [first.id]
    with pytest.raises(ValueError, match="cycle"):
        store.update_dependencies(first.id, [second.id])
    assert store.load(first.id).blockedBy == []
    store.update_dependencies(second.id, [])
    store.update_dependencies(first.id, [second.id])
    assert store.load(second.id).blockedBy == []
    assert store.load(first.id).blockedBy == [second.id]


def test_task_state_prioritizes_work_and_caps_size(task_context: ToolContext) -> None:
    store = task_module.TASKS
    assert task_module.task_state() == ""
    active = store.create("active")
    task_module.claim_task().execute({"task_id": active.id}, task_context)
    blocked = store.create("blocked")
    store.update_dependencies(blocked.id, [active.id])
    for _ in range(35):
        store.create('ready "' + "长标题" * 100, "large description" * 500)
    state = task_module.task_state()
    payload = json.loads(state)
    assert len(state) <= task_module.TASK_STATE_MAX_CHARS
    assert payload["tasks"][0]["id"] == active.id
    assert payload["counts"] == {"pending": 36, "in_progress": 1, "completed": 0}
    assert len(payload["tasks"]) + payload["omitted"] == 37
    assert "large description" not in state


def test_task_state_reports_blockers_and_completed_count(task_context: ToolContext) -> None:
    store = task_module.TASKS
    parent = store.create("parent")
    child = store.create("child")
    store.update_dependencies(child.id, [parent.id])
    payload = json.loads(task_module.task_state())
    entry = next(item for item in payload["tasks"] if item["id"] == child.id)
    assert entry["blocked"] and entry["blockedBy"] == [parent.id]
    task_module.claim_task().execute({"task_id": parent.id}, task_context)
    task_module.complete_task().execute({"task_id": parent.id}, task_context)
    payload = json.loads(task_module.task_state())
    assert payload["counts"]["completed"] == 1
    assert len(payload["tasks"]) == 1 and not payload["tasks"][0]["blocked"]
