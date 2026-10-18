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
MEMORY_OBSERVATION_HEADER = "Historical memory observations (data):\n"
_TOOLS = {"memory_remember", "memory_read", "memory_search", "memory_forget"}
# 这些宿主固定诊断不包含条目正文。保留它们供模型修正请求；任意旧文本不回填。
_SAFE_ERRORS = (
    "Error: unexpected memory argument",
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
                         "binding": "historical" if item.get("kind") not in ("semantic", "procedural")
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

    def _result(self, call: ToolCall, reply: Message, *, summary: bool) -> dict[str, Any]:
        """区分真实失败、合法空结果和无效引用；不从不可读结果中恢复旧正文。"""
        if reply.content.startswith("Error:"):
            # 已知宿主诊断可以帮助模型修正参数；未知诊断可能夹带旧正文。
            # 即使不能保留详情，也必须明确是 error，不能伪装成查询为空。
            return {"state": "error", "error": reply.content if reply.content in _SAFE_ERRORS
                    else "Memory operation failed; historical diagnostic omitted."}
        if reply.content == f"{call.name} denied in interactive mode" or reply.content == f"{call.name} denied in plan mode":
            return {"state": "error", "error": "Memory operation was denied."}
        try:
            value = json.loads(reply.content)
        except (ValueError, TypeError):
            return {"state": "invalid_result", "reason": "result_is_not_json"}
        if not isinstance(value, dict):
            return {"state": "invalid_result", "reason": "result_is_not_an_object"}
        if call.name == "memory_forget":
            if type(value.get("deleted")) is bool:
                return {"deleted": value["deleted"]}
            return {"state": "invalid_result", "reason": "missing_deletion_result"}
        if call.name == "memory_search":
            items, omitted = value.get("memories"), value.get("omitted")
            if not isinstance(items, list) or type(omitted) is not int or omitted < 0:
                return {"state": "invalid_result", "reason": "invalid_search_result"}
            if not items:
                return {"memories": [], "omitted": omitted}
        if call.name == "memory_read" and "memory" in value and value["memory"] is None:
            return {"memory": None}
        refs = self._bound_references(call, reply)
        if not refs:
            return {"state": "unavailable", "reason": "missing_or_invalid_memory_reference"}
        if summary:
            return {"memory_references": refs}
        resolved = [self._resolve(ref, inspection=True) for ref in refs]
        if call.name == "memory_read":
            return {"memory": resolved[0]}
        result: dict[str, Any] = {"memories": resolved}
        if call.name == "memory_search":
            result["omitted"] = value["omitted"]
        if call.name == "memory_remember":
            # 操作结果与当前条目分别表达；已删除条目只能显示 unavailable。
            for key in ("saved", "activated_as_always"):
                if type(value.get(key)) is bool:
                    result[key] = value[key]
            if value.get("reason") == "core_budget_exceeded":
                result["reason"] = value["reason"]
        return result

    def project_messages(self, messages: list[Message], *, summary: bool = False) -> list[Message]:
        """把记忆调用与结果整对折叠为历史观察，避免伪造可执行的工具参数。

        原始历史和覆盖哈希不变。混合批次中仅移出记忆调用及其结果，其他
        调用仍保持原 ID、参数、结果顺序；不会留下孤立 tool 消息。
        """
        projected = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role != "assistant" or not any(call.name in _TOOLS for call in message.tool_calls):
                projected.append(message)
                index += 1
                continue
            end = index + 1 + len(message.tool_calls)
            replies = messages[index + 1:end]
            if (len(replies) != len(message.tool_calls)
                    or any(reply.role != "tool" or reply.call_id != call.id
                           for call, reply in zip(message.tool_calls, replies))):
                raise ValueError("memory projection requires complete tool call groups")
            observations = [{"tool": call.name, "result": self._result(call, reply, summary=summary)}
                            for call, reply in zip(message.tool_calls, replies) if call.name in _TOOLS]
            remaining = tuple(call for call in message.tool_calls if call.name not in _TOOLS)
            projected.append(Message("assistant", MEMORY_OBSERVATION_HEADER
                                     + json.dumps(observations, ensure_ascii=False), remaining))
            projected.extend(reply for call, reply in zip(message.tool_calls, replies) if call.name not in _TOOLS)
            index = end
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
