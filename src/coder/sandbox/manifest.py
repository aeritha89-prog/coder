"""任务归属和导出状态持久化；只认领本项目记录的资源。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from coder.sandbox.base import SandboxError


def task_dir(state_dir: Path, task_id: str) -> Path:
    """把任务 ID 限制为本项目生成的 32 位十六进制串，再定位任务目录。"""
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise SandboxError("invalid sandbox task ID")
    return state_dir / task_id


def save_record(directory: Path, record: dict[str, Any]) -> None:
    """持久化状态和资源归属，返回值为 None。

    临时文件与 record.json 位于同一目录；完成写入和 fsync 后原子替换，
    避免进程中断时留下半份 JSON。finally 清理未替换的临时文件。
    """
    directory.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         delete=False) as output:
            temporary = output.name
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "record.json")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_record(state_dir: Path, task_id: str) -> dict[str, Any]:
    """读取任务记录，并核对目录 ID 与记录 ID 一致后返回字典。"""
    path = task_dir(state_dir, task_id) / "record.json"
    try:
        record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SandboxError(f"sandbox task record is unavailable: {task_id}") from exc
    if record.get("task_id") != task_id:
        raise SandboxError("sandbox task record ownership mismatch")
    return record


def list_records(state_dir: Path) -> list[dict[str, Any]]:
    """列出可识别的任务记录；损坏或不完整目录不影响其他任务展示。"""
    records: list[dict[str, Any]] = []
    if not state_dir.is_dir():
        return records
    for directory in sorted(state_dir.iterdir()):
        if not directory.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
            continue
        try:
            records.append(load_record(state_dir, directory.name))
        except SandboxError:
            continue
    return records
