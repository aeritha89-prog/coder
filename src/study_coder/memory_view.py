"""模型视图中的记忆引用：只改副本，永不改原始工具组或摘要覆盖哈希。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from typing import Any, Protocol

from study_coder.memory_budget import estimate_tokens
from study_coder.types import Message, ToolCall

MAX_REFERENCES = 16
_TOOLS = {"memory_remember", "memory_read", "memory_search", "memory_forget"}
# 这些宿主固定诊断不包含条目正文。保留它们供模型修正请求；任意旧文本不回填。
_SAFE_ERRORS = (
    "Error: updating memory requires expected_revision; read its current version first",
    "Error: memory revision conflict; read the current version before updating",
    "Error: inferred candidates cannot replace active memory; current user evidence is required",
    "Error: memory was withdrawn; new explicit user evidence is required",
    "Error: changing always memory requires evidence quoted from the current user's request",
    "Error: evidence must quote the current user's request",
    "Error: core budget exceeded; existing always memory was not changed",
)


class ReadableStore(Protocol):
    def get(self, identifier: str, revision: int | None = None) -> Any: ...


def valid_reference(value: Any) -> bool:
    """引用只保存有界身份；正文与展开结果不进入会话缓存。"""
    return (isinstance(value, dict) and set(value) == {"id", "revision", "binding"}
            and isinstance(value["id"], str)
            and re.fullmatch(r"mem_[0-9a-f]{16}", value["id"]) is not None
            and type(value["revision"]) is int and value["revision"] > 0
            and value["binding"] in {"current", "historical"})


class MemoryProjection:
    def __init__(self, store: ReadableStore) -> None:
        self.store = store

    @staticmethod
    def _references(call: ToolCall, result: Message) -> list[dict[str, Any]]:
        """只解释匹配记忆工具的结果，普通文件/网络返回的同形 JSON 不被信任。"""
        try:
            value = json.loads(result.content)
        except (ValueError, TypeError):
            return []
        if not isinstance(value, dict):
            return []
        values = value.get("memories", [value.get("memory", value)])
        if not isinstance(values, list):
            return []
        refs = []
        for item in values[:100]:
            if not isinstance(item, dict):
                continue
            reference = {"id": item.get("id"), "revision": item.get("revision"),
                         "binding": "historical" if item.get("kind") not in {"semantic", "procedural"}
                         or call.arguments.get("revision") is not None else "current"}
            if valid_reference(reference):
                refs.append(reference)
        return refs

    def _bound_references(self, call: ToolCall, result: Message) -> list[dict[str, Any]]:
        refs = self._references(call, result)
        for ref in refs:
            try:
                entry = self.store.get(ref["id"])
                # 紧凑结果可能省略 kind，按宿主库确认；显式版本读取始终冻结。
                if entry is not None and call.arguments.get("revision") is None:
                    ref["binding"] = "historical" if entry.kind == "episodic" else "current"
            except (ValueError, TypeError, OSError, sqlite3.Error):
                pass  # 无法读取时保持保守绑定，不取旧正文作回退。
        return refs

    def references_from(self, messages: list[Message]) -> list[dict[str, Any]]:
        calls = {call.id: call for message in messages for call in message.tool_calls
                 if call.name in _TOOLS - {"memory_forget"}}
        refs = []
        for message in messages:
            if message.role != "tool" or message.call_id not in calls:
                continue
            call = calls[message.call_id]  # type: ignore[index]
            refs.extend(self._bound_references(call, message))
        return refs

    def _resolve(self, reference: dict[str, Any], *, inspection: bool = False) -> dict[str, Any]:
        """当前约定跟随当前有效版；历史引用保留当时版本；撤销不可读历史正文。"""
        try:
            entry = self.store.get(reference["id"], reference["revision"]
                                   if reference["binding"] == "historical" else None)
            if entry is None:
                return {"id": reference["id"], "state": "unavailable"}
            if not inspection and reference["binding"] == "current" and entry.status != "active":
                return {"id": entry.id, "revision": entry.revision, "state": entry.status}
            value = asdict(entry)
            value["reference_binding"] = reference["binding"]
            return value
        except (ValueError, TypeError, OSError, sqlite3.Error):
            # 库暂时不可用也不能退回复制旧正文，否则删除可能被旧工具结果绕过。
            return {"id": reference["id"], "state": "unavailable"}

    def project_messages(self, messages: list[Message], *, summary: bool = False) -> list[Message]:
        """保留原始 role、调用 ID、顺序；抹去历史写入参数中的正文和证据副本。"""
        calls = {call.id: call for message in messages for call in message.tool_calls
                 if call.name in _TOOLS}
        projected = []
        for message in messages:
            if message.role == "assistant" and any(call.name in _TOOLS for call in message.tool_calls):
                new_calls = []
                for call in message.tool_calls:
                    args = call.arguments
                    if call.name in _TOOLS:
                        args = {key: value for key, value in args.items()
                                if key in {"id", "revision", "expected_revision", "kind", "limit", "load_policy"}}
                        args["historical_input"] = "omitted; consult memory references"
                    new_calls.append(ToolCall(call.id, call.name, args))
                projected.append(Message(message.role, "", tuple(new_calls), message.call_id))
            elif message.role == "tool" and message.call_id in calls:
                call = calls[message.call_id]  # type: ignore[index]
                if call.name == "memory_forget":
                    projected.append(message)
                    continue
                if not summary and message.content in _SAFE_ERRORS:
                    projected.append(message)
                    continue
                refs = self._bound_references(call, message)
                value: dict[str, Any] = {"memory_references": refs} if summary else {
                    "memories": [self._resolve(ref, inspection=True) for ref in refs]}
                # 失败/损坏的旧结果也不直接复用正文；原始错误仍可在会话归档查看。
                if not refs:
                    value = {"memory_state": "no readable reference; query current memory"}
                projected.append(Message("tool", json.dumps(value, ensure_ascii=False), call_id=message.call_id))
            else:
                projected.append(message)
        return projected

    def resolve_references(self, refs: list[dict[str, Any]], budget: int) -> str:
        """展开后再估算预算，只加入完整引用，绝不把当前正文写回摘要。"""
        selected: list[dict[str, Any]] = []
        for ref in refs[:MAX_REFERENCES]:
            if not valid_reference(ref):
                continue
            entry = self._resolve(ref)
            value: dict[str, Any] = {key: entry[key] for key in ("id", "revision", "kind", "key", "recall_text", "state", "status")
                     if key in entry}
            value["binding"] = ref["binding"]
            candidate = json.dumps(selected + [value], ensure_ascii=False)
            if estimate_tokens([Message("user", "Memory references (data):\n" + candidate)]) <= budget:
                selected.append(value)
        return "Memory references (data):\n" + json.dumps(selected, ensure_ascii=False) if selected else ""
