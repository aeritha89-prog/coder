"""保存对话历史与活动技能版本，正文恢复自可信镜像。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coder.config import Settings
from coder.types import Message, ToolCall


def session_path(root: Path, name: str) -> Path:
    """验证会话名并校验解析后的路径仍在工作区内。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("session name must contain only letters, digits, _ or -")
    path = root / "sessions" / f"{name}.json"
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("session path escapes workspace")
    return path


def save_session(root: Path, name: str, settings: Settings, history: list[Message],
                 active_skills: dict[str, str] | None = None,
                 memory_state: dict[str, Any] | None = None) -> Path:
    """原子保存完整消息和技能版本，返回最终会话路径。

    root 是宿主保存目录；history 含原始 assistant/tool 调用组，保留 call_id。
    active_skills 保存名称到正文摘要的映射，不保存权限、压缩缓存或 API key。
    后台结果交付依赖此函数成功返回，不能先标记已读取再写会话。
    """
    path = session_path(root, name)
    path.parent.mkdir(exist_ok=True)
    # payload 是会话协议，区别于后台 runner 的 record 和管理器的 index。
    payload = {"version": 3, "created_at": datetime.now(timezone.utc).isoformat(),
               "workspace": str(settings.workspace), "model": settings.model,
               "history": [asdict(message) for message in history],
               "active_skills": dict(active_skills or {}), "memory": memory_state}
    temporary: str | None = None  # 同目录临时文件名，供替换成功或异常后清理。
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as output:
            # delete=False 允许关闭文件句柄后再替换，适配 Windows 的文件共享限制。
            temporary = output.name
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        # 先 flush/fsync，再替换；写入失败时旧会话仍可用，不留下半份 history。
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_session_bundle(root: Path, name: str, settings: Settings
                        ) -> tuple[list[Message], dict[str, str], Any]:
    """读取完整会话及短期层；没有缓存的会话仍可从完整历史重新摘要。"""
    payload: dict[str, Any] = json.loads(session_path(root, name).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 3 or payload.get("workspace") != str(settings.workspace):
        raise ValueError("session version or workspace does not match")
    if not {"history", "active_skills", "memory"} <= payload.keys():
        raise ValueError("current session requires history, active_skills and memory fields")
    messages: list[Message] = []
    for raw in payload["history"]:
        calls = tuple(ToolCall(**item) for item in raw["tool_calls"])
        messages.append(Message(raw["role"], raw["content"], calls, raw["call_id"]))
    skills = payload["active_skills"]
    if not isinstance(skills, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in skills.items()
    ):
        raise ValueError("invalid session active_skills")
    return messages, skills, payload["memory"]
