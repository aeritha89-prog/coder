"""三层记忆的数据结构和项目长期记忆；所有持久化都在宿主执行。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from study_coder.memory.budget import core_tokens, core_value, effective_budget, memory_message
from study_coder.memory.schema import (
    fingerprint,
    initialize_schema,
    record_change,
)
from study_coder.types import Message, ToolResult

MEMORY_KINDS = ("semantic", "episodic", "procedural")
LOAD_POLICIES = ("always", "on_demand")
MEMORY_STATUSES = ("active", "candidate", "stale", "archived", "superseded", "deleted")
MEMORY_TOOLS = frozenset({"memory_search", "memory_read", "memory_remember", "memory_forget"})
# 只按明确格式或字段名识别凭据；普通哈希、变量名不能仅凭长度就判成密钥。
# 私钥缺少 END 时一直覆盖到末尾，防止截断过的输出泄露剩余正文。
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----"
    r".*?(?:-----END (?P=label)-----|\Z)", re.DOTALL | re.IGNORECASE,
)
_SECRET = re.compile(
    r"""(?i)\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"""
    r"""auth[_-]?token|token|password|passwd|secret|client[_-]?secret|"""
    r"""aws[_-]?secret[_-]?access[_-]?key|private[_-]?key)["']?\s*[=:]\s*"""
    r"""(?:"(?:\\.|[^"\\\r\n])*(?:"|(?=\r?\n|\Z))|"""
    r"""'(?:\\.|[^'\\\r\n])*(?:'|(?=\r?\n|\Z))|[^\s,;}\]]+)"""
    r"|\b(?:Bearer|Basic)\s+\S+|\bsk-[A-Za-z0-9_-]{12,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
    r"|\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
    r"|https?://[^\s/@]+:[^\s/@]+@"
)


def _contains_credentials(text: str) -> bool:
    return bool(_PRIVATE_KEY.search(text) or _SECRET.search(text))


def _redact_credentials(text: str) -> str:
    """先移除整块私钥，再处理字段、认证头、令牌与 URL 中的凭据。"""
    return _SECRET.sub("[redacted]", _PRIVATE_KEY.sub("[redacted private key]", text))


@dataclass(frozen=True)
class WorkingMemory:
    """当前请求的活动内容；进程和任务状态每轮重查，不从旧会话复活。"""

    goal: str = ""
    task_state: str = ""
    background_state: str = ""
    messages: tuple[Message, ...] = ()


@dataclass(frozen=True)
class ShortTermMemory:
    """会话摘要及它覆盖的原始消息组哈希；可随会话一起持久化。"""

    summary: str = ""
    covered_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryEntry:
    """长期条目：种类、稳定键、内容、检索标签、来源和时间。"""

    id: str
    kind: str
    key: str
    content: str
    tags: list[str]
    source: str
    created_at: float
    updated_at: float
    load_policy: str = "on_demand"
    priority: int = 50
    status: str = "active"
    revision: int = 1
    source_kind: str = "user_action"
    source_refs: list[dict[str, str]] = field(default_factory=list)
    recall_text: str = ""
    scope_type: str = "workspace"
    scope_id: str = ""
    verification_status: str = "unknown"
    verified_at: float | None = None
    conditions: dict[str, Any] = field(default_factory=dict)
    expires_at: float | None = None
    content_fingerprint: str = ""
    fingerprint_version: int = 1
    supersedes_revision: int | None = None


@dataclass(frozen=True)
class MemoryContext:
    """常驻资料与按需知识分开传递，便于独立预算和按顺序降级。"""

    core: str = ""
    related: str = ""


def _redacted_entry(entry: MemoryEntry) -> MemoryEntry:
    """读取副本始终脱敏；保留 ID 供用户删除，不回写或改动历史归档。"""
    return replace(entry, key=_redact_credentials(entry.key),
                   content=_redact_credentials(entry.content),
                   tags=[_redact_credentials(tag) for tag in entry.tags],
                   source=_redact_credentials(entry.source),
                   recall_text=_redact_credentials(entry.recall_text),
                   conditions=_redact_value(entry.conditions),
                   source_refs=[{key: _redact_credentials(value) for key, value in ref.items()}
                                for ref in entry.source_refs])


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_credentials(value)
    if isinstance(value, dict):
        return {_redact_credentials(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise ValueError(f"{field} must be nonempty text of at most {maximum} characters")
    return value.strip()


def _terms(text: str) -> set[str]:
    """英文按词、中文按连续双字切分；不依赖 embedding 或第三方分词服务。"""
    terms = set(re.findall(r"[a-z0-9_./-]{2,}", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return terms


def _fingerprint(kind: str, content: str) -> str:
    """事实指纹保留大小写与否定；情景仅去掉已知的调用追踪字段。"""
    return fingerprint(kind, content)


class MemoryStore:
    """每个真实工作区一份 SQLite；事务处理并发写入，不修改项目工作文件。"""

    def __init__(self, state_dir: Path, workspace: Path, max_entries: int = 1000) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= 10_000:
            raise ValueError("memory max_entries must be 1..10000")
        self.workspace = os.path.normcase(str(workspace.resolve(strict=True)))
        identity = hashlib.sha256(self.workspace.encode("utf-8")).hexdigest()
        self.path = state_dir / "memory" / identity / "memory.sqlite3"
        self.max_entries = max_entries

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            initialize_schema(connection, self.workspace)
            # 建库后校验归属；当前格式库不在每次查询时重复 DDL 或写事务。
            owner = connection.execute("SELECT value FROM metadata WHERE key='workspace'").fetchone()
            if owner is None or owner[0] != self.workspace:
                raise ValueError("memory workspace does not match")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _entry(row: Any) -> MemoryEntry:
        if any(not isinstance(row[key], str) for key in ("tags", "source_refs", "conditions")):
            raise ValueError("invalid long-term memory entry")
        tags = json.loads(row["tags"])
        if (row["kind"] not in MEMORY_KINDS or not isinstance(tags, list)
                or any(not isinstance(tag, str) for tag in tags)
                or not isinstance(row["content"], str) or not isinstance(row["key"], str)
                or not isinstance(row["source"], str)
                or row["load_policy"] not in LOAD_POLICIES
                or (row["kind"] == "episodic" and row["load_policy"] == "always")
                or type(row["priority"]) is not int or not 0 <= row["priority"] <= 100):
            raise ValueError("invalid long-term memory entry")
        refs = json.loads(row["source_refs"])
        conditions = json.loads(row["conditions"])
        if (row["status"] not in MEMORY_STATUSES or type(row["revision"]) is not int
                or row["revision"] < 1 or not isinstance(row["recall_text"], str)
                or not isinstance(refs, list) or len(refs) > 8
                or any(not isinstance(ref, dict) or set(ref) != {"kind", "event_id"}
                       or any(not isinstance(value, str) for value in ref.values()) for ref in refs)
                or not isinstance(conditions, dict) or row["scope_type"] != "workspace"
                or not isinstance(row["scope_id"], str)
                or row["source_kind"] not in {"legacy", "user_action", "user_statement", "model_inferred", "tool_observed"}
                or row["verification_status"] not in {"unknown", "observed"}
                or not isinstance(row["content_fingerprint"], str)):
            raise ValueError("invalid long-term memory entry")
        return MemoryEntry(row["id"], row["kind"], row["key"], row["content"],
                           tags, row["source"], row["created_at"], row["updated_at"],
                           row["load_policy"], row["priority"], row["status"], row["revision"],
                           row["source_kind"], refs, row["recall_text"], row["scope_type"],
                           row["scope_id"], row["verification_status"], row["verified_at"],
                           conditions, row["expires_at"], row["content_fingerprint"],
                           row["fingerprint_version"], row["supersedes_revision"])

    def remember(self, kind: str, key: str, content: str, tags: Any = None,
                 *, source: str = "manual", load_policy: str | None = None,
                 priority: int | None = None, allow_always: bool = True,
                 core_budget: int | None = None, recall_text: str | None = None,
                 status: str = "active", source_kind: str = "user_action",
                 source_refs: list[dict[str, str]] | None = None,
                 expected_revision: int | None = None, require_revision: bool = False) -> MemoryEntry:
        """同类同键更新，重复正文复用原条目；容量满时只淘汰最旧情景记录。"""
        if kind not in MEMORY_KINDS:
            raise ValueError("kind must be semantic, episodic or procedural")
        if load_policy is not None and load_policy not in LOAD_POLICIES:
            raise ValueError("load_policy must be always or on_demand")
        if priority is not None and (type(priority) is not int or not 0 <= priority <= 100):
            raise ValueError("priority must be an integer from 0 to 100")
        key = _text(key, "key", 120).casefold()
        content = _text(content, "content", 2000)
        recall_text = content if recall_text is None else _text(recall_text, "recall_text", 2000)
        source = _text(source, "source", 300)
        if status not in {"active", "candidate"}:
            raise ValueError("new memory status must be active or candidate")
        if source_kind not in {"user_action", "user_statement", "model_inferred", "tool_observed"}:
            raise ValueError("invalid memory source kind")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("expected_revision must be a positive integer")
        refs = [] if source_refs is None else source_refs
        if (not isinstance(refs, list) or len(refs) > 8
                or any(not isinstance(ref, dict) or set(ref) != {"kind", "event_id"}
                       or any(not isinstance(value, str) or not value or len(value) > 300
                              for value in ref.values()) for ref in refs)):
            raise ValueError("invalid memory source references")
        if any(_contains_credentials(text) for text in
               (content, recall_text, key, source, json.dumps(refs))):
            raise ValueError("memory must not contain credentials; use environment variable names instead")
        tags = [] if tags is None else tags
        if (not isinstance(tags, list) or len(tags) > 8
                or any(not isinstance(t, str) or not t.strip() or len(t) > 64 for t in tags)):
            raise ValueError("tags must contain at most 8 nonempty strings of at most 64 characters")
        tags = list(dict.fromkeys(t.strip().casefold() for t in tags))
        if _contains_credentials(" ".join(tags)):
            raise ValueError("memory tags must not contain credentials; use environment variable names instead")
        digest = hashlib.sha256(" ".join(content.casefold().split()).encode("utf-8")).hexdigest()
        fingerprint = _fingerprint(kind, content)
        connection = self._connect()
        try:
            # BEGIN IMMEDIATE 覆盖查询、容量检查和写入，避免两个进程同时占最后一格。
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM memories WHERE kind=? AND key=?",
                                         (kind, key)).fetchone()
                if row is not None and row["status"] == "deleted":
                    connection.execute("DELETE FROM memories WHERE id=?", (row["id"],))
                    row = None
                if row is None:
                    row = connection.execute("SELECT * FROM memories WHERE kind=? AND digest=? AND status!='deleted'",
                                             (kind, digest)).fetchone()
                if row is not None and self._entry(row).scope_id != self.workspace:
                    raise ValueError("memory entry scope does not match")
                if row is not None and require_revision and expected_revision is None:
                    raise ValueError("updating memory requires expected_revision; read its current version first")
                if expected_revision is not None and (row is None or row["revision"] != expected_revision):
                    raise ValueError("memory revision conflict; read the current version before updating")
                if row is not None and row["status"] == "active" and source_kind == "model_inferred":
                    raise ValueError("inferred candidates cannot replace active memory; current user evidence is required")
                if row is not None and row["status"] == "archived":
                    count = connection.execute("SELECT COUNT(*) FROM memories WHERE status IN ('active','candidate','stale')").fetchone()[0]
                    if count >= self.max_entries:
                        raise ValueError("long-term memory is full; archive or forget an entry first")
                policy = load_policy if load_policy is not None else (row["load_policy"] if row else "on_demand")
                importance = priority if priority is not None else (row["priority"] if row else 50)
                if policy == "always" and (kind == "episodic" or len(recall_text) > 500):
                    raise ValueError("always memory must be semantic/procedural and at most 500 characters")
                # 同键更新和正文去重都不能绕过常驻条目的用户来源检查。
                if (policy == "always" or (row and row["load_policy"] == "always")) and not allow_always:
                    raise ValueError("changing always memory requires evidence quoted from the current user's request")
                identifier = row["id"] if row else "mem_" + uuid.uuid4().hex[:16]
                # 检查在事务内完成，避免校验后其他写入改变匹配到的条目。
                if policy == "always" and core_budget is not None:
                    if core_tokens(identifier, row["key"] if row else key, recall_text) > core_budget:
                        if row and row["load_policy"] == "always":
                            raise ValueError("core budget exceeded; existing always memory was not changed")
                        policy = "on_demand"
                now = time.time()
                if row is not None:
                    connection.execute("UPDATE memories SET content=?,tags=?,source=?,updated_at=?,digest=?,load_policy=?,priority=?,status=?,supersedes_revision=revision,revision=revision+1,source_kind=?,source_refs=?,recall_text=?,content_fingerprint=?,verification_status=? WHERE id=?",
                                       (content, json.dumps(tags, ensure_ascii=False), source, now, digest,
                                         policy, importance, status, source_kind, json.dumps(refs),
                                         recall_text, fingerprint,
                                         "observed" if source_kind == "tool_observed" else "unknown", row["id"]))
                    identifier = row["id"]
                else:
                    count = connection.execute("SELECT COUNT(*) FROM memories WHERE status IN ('active','candidate','stale')").fetchone()[0]
                    if count >= self.max_entries:
                        oldest = connection.execute("SELECT id FROM memories WHERE kind='episodic' AND status IN ('active','candidate','stale') ORDER BY updated_at LIMIT 1").fetchone()
                        if oldest is None:
                            raise ValueError("long-term memory is full; forget an entry before adding another")
                        connection.execute("DELETE FROM memories WHERE id=?", (oldest[0],))
                        connection.execute("DELETE FROM memory_versions WHERE memory_id=?", (oldest[0],))
                    connection.execute("INSERT INTO memories (id,kind,key,content,tags,source,created_at,updated_at,digest,load_policy,priority) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                       (identifier, kind, key, content, json.dumps(tags, ensure_ascii=False),
                                        source, now, now, digest, policy, importance))
                    connection.execute("UPDATE memories SET status=?,source_kind=?,source_refs=?,recall_text=?,scope_id=?,content_fingerprint=?,verification_status=? WHERE id=?",
                                       (status, source_kind, json.dumps(refs), recall_text, self.workspace,
                                        fingerprint, "observed" if source_kind == "tool_observed" else "unknown", identifier))
                record_change(connection, identifier, "remember")
                saved = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
            return self._entry(saved)
        finally:
            connection.close()

    def search(self, query: str = "", *, kind: str | None = None, limit: int = 10,
               recall: bool = False, load_policy: str | None = None,
               status: str | None = None) -> list[MemoryEntry]:
        """普通知识按关键词重叠排序；常驻资料由 load_always 单独加载。"""
        if not isinstance(query, str) or len(query) > 8000:
            raise ValueError("query must be text of at most 8000 characters")
        if kind is not None and kind not in MEMORY_KINDS:
            raise ValueError("invalid memory kind")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        if load_policy is not None and load_policy not in LOAD_POLICIES:
            raise ValueError("load_policy must be always or on_demand")
        if status is not None and status not in MEMORY_STATUSES:
            raise ValueError("invalid memory status")
        if not self.path.exists():
            return []
        connection = self._connect()
        try:
            clauses, parameters = [], []
            clauses.append("status=?" if recall or status else "status!='deleted'")
            if recall or status:
                parameters.append("active" if recall else status)
            if kind is not None:
                clauses.append("kind=?")
                parameters.append(kind)
            policy = "on_demand" if recall else load_policy
            if policy is not None:
                clauses.append("load_policy=?")
                parameters.append(policy)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute("SELECT * FROM memories" + where + " ORDER BY updated_at DESC, id",
                                      parameters).fetchall()
        finally:
            connection.close()
        terms = _terms(query)
        ranked = []
        for row in rows:
            entry = self._entry(row)
            if entry.scope_id != self.workspace:
                raise ValueError("memory entry scope does not match")
            score = len(terms & _terms(entry.key + " " + entry.content + " " + " ".join(entry.tags)))
            if query.strip() and not score:
                continue
            ranked.append((score, entry.updated_at, entry))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        # 排序后再处理少量返回条目，保护读取出口且不重复扫描整库。
        return [_redacted_entry(entry) for _, _, entry in ranked[:limit]]

    def load_always(self, limit: int = 8) -> tuple[list[MemoryEntry], int]:
        """按优先级、更新时间读取常驻条目，不参与关键词筛选或普通 top_k 竞争。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("always limit must be 1..100")
        if not self.path.exists():
            return [], 0
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM memories WHERE load_policy='always' AND status='active'").fetchone()[0]
            rows = connection.execute("SELECT * FROM memories WHERE load_policy='always' AND status='active' "
                                      "ORDER BY priority DESC, updated_at DESC, id LIMIT ?", (limit,)).fetchall()
            entries = [self._entry(row) for row in rows]
            if any(entry.scope_id != self.workspace for entry in entries):
                raise ValueError("memory entry scope does not match")
            return [_redacted_entry(entry) for entry in entries], total
        finally:
            connection.close()

    @staticmethod
    def _validate_id(identifier: str) -> None:
        if not isinstance(identifier, str) or not re.fullmatch(r"mem_[0-9a-f]{16}", identifier):
            raise ValueError("invalid memory id")

    def get(self, identifier: str, revision: int | None = None) -> MemoryEntry | None:
        """读取当前条目或保留的历史版本；已撤销条目的历史正文也不可恢复。"""
        self._validate_id(identifier)
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("revision must be a positive integer")
        if not self.path.exists():
            return None
        connection = self._connect()
        try:
            current = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
            if current is None or current["status"] == "deleted":
                return None
            if revision is not None and current["revision"] != revision:
                saved = connection.execute("SELECT snapshot FROM memory_versions WHERE memory_id=? AND revision=?",
                                           (identifier, revision)).fetchone()
                if saved is None:
                    return None
                value = json.loads(saved[0])
                value["status"] = "superseded"
            else:
                value = current
            entry = self._entry(value)
            if entry.scope_id != self.workspace:
                raise ValueError("memory entry scope does not match")
            return _redacted_entry(entry)
        finally:
            connection.close()

    def revision(self) -> int:
        """库修订号供后续请求检查变化，读取本身不产生写事务。"""
        if not self.path.exists():
            return 0
        connection = self._connect()
        try:
            return int(connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])
        finally:
            connection.close()

    def set_status(self, identifier: str, status: str, expected_revision: int | None = None) -> MemoryEntry:
        """用户管理候选、归档和待复验条目；激活不等于验证通过。"""
        self._validate_id(identifier)
        if status not in {"active", "candidate", "archived", "stale"}:
            raise ValueError("invalid status transition")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("expected_revision must be a positive integer")
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
                if row is None or row["status"] == "deleted":
                    raise ValueError("memory not found")
                if expected_revision is not None and row["revision"] != expected_revision:
                    raise ValueError("memory revision conflict")
                if status != row["status"]:
                    counted = {"active", "candidate", "stale"}
                    if status in counted and row["status"] not in counted:
                        count = connection.execute("SELECT COUNT(*) FROM memories WHERE status IN ('active','candidate','stale')").fetchone()[0]
                        if count >= self.max_entries:
                            raise ValueError("long-term memory is full; archive or forget an entry first")
                    if status == "archived":
                        count = connection.execute("SELECT COUNT(*) FROM memories WHERE status='archived'").fetchone()[0]
                        if count >= self.max_entries:
                            raise ValueError("archived memory capacity exceeded")
                    connection.execute("UPDATE memories SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                                       (status, time.time(), identifier))
                    record_change(connection, identifier, "status:" + status)
            entry = self.get(identifier)
            assert entry is not None
            return entry
        finally:
            connection.close()

    def forget(self, identifier: str) -> bool:
        """清除当前条目及保留版本；旧 ID 失效，后续允许按正常流程重新保存。"""
        self._validate_id(identifier)
        if not self.path.exists():
            return False
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
                if row is None or row["status"] == "deleted":
                    return False
                connection.execute("DELETE FROM memory_versions WHERE memory_id=?", (identifier,))
                # 保留不可读的旧 ID，释放原 key；再次保存会分配新 ID，旧引用继续失效。
                connection.execute("UPDATE memories SET key=?,content='',recall_text='',tags='[]',source='',source_refs='[]',conditions='{}',digest='',content_fingerprint='',status='deleted',revision=revision+1 WHERE id=?",
                                   ("\0" + identifier, identifier))
                record_change(connection, identifier, "forget")
                return True
        finally:
            connection.close()


class MemoryManager:
    """连接自动回忆、显式知识写入和任务情景归档；不另发模型请求。"""

    def __init__(self, store: MemoryStore, max_context_tokens: int = 1200,
                 top_k: int = 6, auto_episode: bool = True, *,
                 always_max_context_tokens: int = 256, always_max_entries: int = 8,
                 context_window_tokens: int = 32_000) -> None:
        self.store = store
        self.max_context_tokens = max_context_tokens
        self.top_k = top_k
        self.auto_episode = auto_episode
        self.always_max_context_tokens = always_max_context_tokens
        self.always_max_entries = always_max_entries
        self.context_window_tokens = context_window_tokens
        self.origin = "API caller; no session checkpoint supplied"
        self.user_request: str | None = None

    def remember(self, kind: str, key: str, content: str, tags: Any = None,
                 **options: Any) -> dict[str, Any]:
        """模型与 CLI 共用的写入入口；返回实际策略，不能只报告保存成功。"""
        requested = options.get("load_policy")
        entry = self.store.remember(kind, key, content, tags,
            core_budget=effective_budget(self.max_context_tokens, self.context_window_tokens,
                                         self.always_max_context_tokens), **options)
        result = {**asdict(entry), "saved": True,
                  "activated_as_always": entry.load_policy == "always" and entry.status == "active"}
        if requested is not None:
            result["requested_load_policy"] = requested
        if requested == "always" and entry.load_policy != "always":
            result["reason"] = "core_budget_exceeded"
        return result

    @staticmethod
    def _pack(entries: list[MemoryEntry], context: Any, budget: int, *,
              core: bool = False) -> tuple[str, int, int]:
        """只加入完整条目；用于检索排序的时间、优先级不重复占用模型输入。"""
        selected: list[dict[str, Any]] = []
        for entry in entries:
            # 常驻视图只携带可回查 ID、稳定键和正文，给自由表达的偏好留出空间。
            # 完整来源、类别等仍保存在库中，可用 memory_search 回查。
            value: dict[str, Any] = core_value(entry.id, entry.key, entry.recall_text)
            if not core:
                value.update(kind=entry.kind, tags=entry.tags, source=entry.source,
                             updated_at=entry.updated_at)
            candidate = selected + [value]
            encoded = json.dumps(candidate, ensure_ascii=False)
            if context.estimate_tokens([memory_message(encoded, core=core)]) > budget:
                continue
            selected = candidate
        encoded = json.dumps(selected, ensure_ascii=False) if selected else ""
        tokens = context.estimate_tokens([memory_message(encoded, core=core)]) if encoded else 0
        return encoded, tokens, len(selected)

    @staticmethod
    def _log(message: str) -> None:
        print("[记忆] " + message, file=sys.stderr, flush=True)

    def recall(self, query: str, context: Any, *, budget: int | None = None) -> str:
        """普通知识按需召回；常驻条目不占用此路径的 top_k。"""
        total = effective_budget(self.max_context_tokens, context.settings.context_window_tokens)
        limit = total if budget is None else min(total, max(0, budget))
        entries = self.store.search(query[:8000], limit=self.top_k, recall=True)
        encoded, tokens, count = self._pack(entries, context, limit)
        if entries:
            self._log(f"按需：候选 {len(entries)} 项，注入 {count} 项，预算跳过 {len(entries) - count} 项，"
                      f"估算 {tokens}/{limit} token")
        return encoded

    def build_context(self, query: str, context: Any) -> MemoryContext:
        """在总预算内先装常驻资料，再将剩余额度交给相关知识；两路失败分别降级。"""
        total = effective_budget(self.max_context_tokens, context.settings.context_window_tokens)
        core, used, related = "", 0, ""
        try:
            entries, available = self.store.load_always(self.always_max_entries)
            budget = min(total, self.always_max_context_tokens)
            core, used, count = self._pack(entries, context, budget, core=True)
            if available:
                self._log(f"常驻：共 {available} 项，注入 {count} 项，"
                          f"数量跳过 {available - len(entries)} 项，预算跳过 {len(entries) - count} 项，"
                          f"估算 {used}/{budget} token")
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._log(f"本轮跳过常驻资料：{exc}")
        try:
            related = self.recall(query, context, budget=total - used)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._log(f"本轮跳过按需资料：{exc}")
        return MemoryContext(core, related)

    def record_episode(self, goal: str, results: list[ToolResult], outcome: str,
                       source: str) -> None:
        """归档实际工具观察；输出仍是不可信资料，不把助手自述当成验证结果。"""
        if not self.auto_episode or not results:
            return
        # 必须先对完整输出脱敏，再取预览；截断可能破坏私钥块或令牌格式。
        observations = [f"{r.name} (call_id={_redact_credentials(r.call_id)}): "
                        f"{'error' if r.is_error else 'ok'}; {_redact_credentials(r.content)[:300]}"
                        for r in results[-4:] if r.name not in MEMORY_TOOLS]
        if not observations:
            return
        content = (f"Goal: {_redact_credentials(goal)[:350]}\nTurn outcome: {outcome}\n"
                   "Untrusted tool observations (verify before reuse):\n" + "\n".join(observations))
        # 原始日志仍在会话/后台归档；长期层不复制常见凭据。
        content = _redact_credentials(content)[:2000]
        episode_key = "episode:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
        # 去掉 run() 的随机归档编号；任务/消息区间和实际调用编号才是事件身份。
        stable_source = re.sub(r";[0-9a-f]{32}$", "", source)
        refs = [{"kind": "tool_observation", "event_id": hashlib.sha256(
            (stable_source + ";" + r.call_id + ";" + r.name).encode()).hexdigest()}
            for r in results[-4:] if r.name not in MEMORY_TOOLS]
        self.store.remember("episodic", episode_key, content,
                            sorted({r.name for r in results if r.name not in MEMORY_TOOLS})[:8],
                            source=("tool observations:" + source)[:300],
                            source_kind="tool_observed", source_refs=refs)

    def execute(self, name: str, arguments: dict[str, Any], maximum: int) -> ToolResult:
        """模型只能操作当前项目的条目，不能提交宿主路径或修改存储位置。"""
        try:
            if maximum < 160:
                raise ValueError("max_tool_output_chars must be at least 160 for memory tools")
            if name == "memory_search":
                allowed = {"query", "kind", "limit", "load_policy", "status"}
                if set(arguments) - allowed:
                    raise ValueError("unexpected memory argument")
                entries = self.store.search(arguments.get("query", ""),
                                            kind=arguments.get("kind"), limit=arguments.get("limit", 10),
                                            load_policy=arguments.get("load_policy"),
                                            status=arguments.get("status"))
                values: Any = {"memories": [asdict(entry) for entry in entries], "omitted": 0}
            elif name == "memory_read":
                if set(arguments) - {"id", "revision"} or "id" not in arguments:
                    raise ValueError("memory_read requires id and optional revision")
                entry = self.store.get(arguments["id"], arguments.get("revision"))
                values = {"memory": asdict(entry) if entry is not None else None}
            elif name == "memory_remember":
                if set(arguments) - {"kind", "key", "content", "tags", "load_policy", "priority", "evidence", "recall_text", "expected_revision"}:
                    raise ValueError("unexpected memory argument")
                evidence = arguments.get("evidence")
                verified = False
                source = "agent note; verify before reuse; " + self.origin
                if evidence is not None:
                    quote = _text(evidence, "evidence", 1000)
                    if self.user_request is None or quote not in self.user_request:
                        raise ValueError("evidence must quote the current user's request")
                    # 来源由宿主绑定实际用户输入，不接受模型自行声明 source=user。
                    verified = True
                    source = ("user request quote: " + _redact_credentials(quote)[:100]
                              + "; " + self.origin)[:300]
                values = self.remember(arguments.get("kind", ""), arguments.get("key", ""),
                    arguments.get("content", ""), arguments.get("tags"),
                    source=source, load_policy=arguments.get("load_policy"),
                    priority=arguments.get("priority"), allow_always=verified,
                    recall_text=arguments.get("recall_text"),
                    expected_revision=arguments.get("expected_revision"), require_revision=True,
                    source_kind="user_statement" if verified else "model_inferred",
                    status="active" if verified else "candidate",
                    source_refs=[{"kind": "user_message", "event_id": hashlib.sha256(
                        (self.origin + "\n" + (self.user_request or "")).encode()).hexdigest()}])
            elif name == "memory_forget":
                if set(arguments) != {"id"}:
                    raise ValueError("memory_forget requires only id")
                values = {"deleted": self.store.forget(arguments["id"])}
            else:
                raise ValueError("unknown memory tool")
            encoded = json.dumps(values, ensure_ascii=False)
            # 结果预算不影响已提交内容；用稳定 ID 和明确标志代替半份 JSON。
            if len(encoded) > maximum:
                if "memories" in values:
                    while values["memories"] and len(json.dumps(values, ensure_ascii=False)) > maximum:
                        values["memories"].pop()
                        values["omitted"] += 1
                    encoded = json.dumps(values, ensure_ascii=False)
                elif name == "memory_remember":
                    # 最小预算也保留真实加载方式；详细元数据可以按 ID 回查。
                    compact = {key: values[key] for key in
                               ("id", "revision", "saved", "load_policy", "activated_as_always", "reason")
                               if key in values}
                    # 版本和实际策略始终保留；预算允许时再带候选状态。
                    detail = {**compact, "status": values["status"]}
                    if len(json.dumps(detail, separators=(",", ":"))) <= maximum:
                        compact = detail
                    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
                elif name == "memory_read" and values["memory"] is not None:
                    item = values["memory"]
                    encoded = json.dumps({"id": item["id"], "revision": item["revision"],
                                          "status": item["status"], "truncated": True}, separators=(",", ":"))
            if len(encoded) > maximum:
                raise ValueError("max_tool_output_chars is too small for memory metadata")
            return ToolResult("", name, encoded)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return ToolResult("", name, f"Error: {exc}", True)
