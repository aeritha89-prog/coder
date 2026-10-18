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
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from study_coder.memory_budget import core_tokens, core_value, effective_budget, memory_message
from study_coder.types import Message, ToolResult

MEMORY_KINDS = ("semantic", "episodic", "procedural")
LOAD_POLICIES = ("always", "on_demand")
_SCHEMA_VERSION = 1
MEMORY_TOOLS = frozenset({"memory_search", "memory_remember", "memory_forget"})
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


@dataclass(frozen=True)
class MemoryContext:
    """常驻资料与按需知识分开传递，便于独立预算和按顺序降级。"""

    core: str = ""
    related: str = ""


def _redacted_entry(entry: MemoryEntry) -> MemoryEntry:
    """旧库只在返回副本中脱敏；保留 ID 供用户删除，不回写或改动历史归档。"""
    return replace(entry, key=_redact_credentials(entry.key),
                   content=_redact_credentials(entry.content),
                   tags=[_redact_credentials(tag) for tag in entry.tags],
                   source=_redact_credentials(entry.source))


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
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise ValueError("memory database schema is newer than this application")
            if version < _SCHEMA_VERSION:
                # 迁移与首次建库共用事务。拿锁后再次检查，允许多个进程同时启动。
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
                        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
                        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('workspace', ?)",
                                           (self.workspace,))
                        owner = connection.execute("SELECT value FROM metadata WHERE key='workspace'").fetchone()[0]
                        if owner != self.workspace:
                            raise ValueError("memory workspace does not match")
                        connection.execute("""CREATE TABLE IF NOT EXISTS memories (
                            id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
                            content TEXT NOT NULL, tags TEXT NOT NULL, source TEXT NOT NULL,
                            created_at REAL NOT NULL, updated_at REAL NOT NULL, digest TEXT NOT NULL,
                            UNIQUE(kind, key))""")
                        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memories)")}
                        if "load_policy" not in columns:
                            connection.execute("ALTER TABLE memories ADD COLUMN load_policy TEXT NOT NULL DEFAULT 'on_demand'")
                        if "priority" not in columns:
                            connection.execute("ALTER TABLE memories ADD COLUMN priority INTEGER NOT NULL DEFAULT 50")
                        connection.execute("CREATE INDEX IF NOT EXISTS memories_loading ON memories(load_policy, priority, updated_at)")
                        connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            # 已迁移的库走只读校验，不在每次查询时重复 DDL 或发起写事务。
            if connection.execute("SELECT value FROM metadata WHERE key='workspace'").fetchone()[0] != self.workspace:
                raise ValueError("memory workspace does not match")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _entry(row: sqlite3.Row) -> MemoryEntry:
        tags = json.loads(row["tags"])
        if (row["kind"] not in MEMORY_KINDS or not isinstance(tags, list)
                or any(not isinstance(tag, str) for tag in tags)
                or not isinstance(row["content"], str) or not isinstance(row["key"], str)
                or not isinstance(row["source"], str)
                or row["load_policy"] not in LOAD_POLICIES
                or (row["kind"] == "episodic" and row["load_policy"] == "always")
                or type(row["priority"]) is not int or not 0 <= row["priority"] <= 100):
            raise ValueError("invalid long-term memory entry")
        return MemoryEntry(row["id"], row["kind"], row["key"], row["content"],
                            tags, row["source"], row["created_at"], row["updated_at"],
                            row["load_policy"], row["priority"])

    def remember(self, kind: str, key: str, content: str, tags: Any = None,
                 *, source: str = "manual", load_policy: str | None = None,
                 priority: int | None = None, allow_always: bool = True,
                 core_budget: int | None = None) -> MemoryEntry:
        """同类同键更新，重复正文复用原条目；容量满时只淘汰最旧情景记录。"""
        if kind not in MEMORY_KINDS:
            raise ValueError("kind must be semantic, episodic or procedural")
        if load_policy is not None and load_policy not in LOAD_POLICIES:
            raise ValueError("load_policy must be always or on_demand")
        if priority is not None and (type(priority) is not int or not 0 <= priority <= 100):
            raise ValueError("priority must be an integer from 0 to 100")
        key = _text(key, "key", 120).casefold()
        content = _text(content, "content", 2000)
        source = _text(source, "source", 300)
        if any(_contains_credentials(text) for text in (content, key, source)):
            raise ValueError("memory must not contain credentials; use environment variable names instead")
        tags = [] if tags is None else tags
        if (not isinstance(tags, list) or len(tags) > 8
                or any(not isinstance(t, str) or not t.strip() or len(t) > 64 for t in tags)):
            raise ValueError("tags must contain at most 8 nonempty strings of at most 64 characters")
        tags = list(dict.fromkeys(t.strip().casefold() for t in tags))
        if _contains_credentials(" ".join(tags)):
            raise ValueError("memory tags must not contain credentials; use environment variable names instead")
        digest = hashlib.sha256(" ".join(content.casefold().split()).encode("utf-8")).hexdigest()
        connection = self._connect()
        try:
            # BEGIN IMMEDIATE 覆盖查询、容量检查和写入，避免两个进程同时占最后一格。
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM memories WHERE kind=? AND key=?",
                                         (kind, key)).fetchone()
                if row is None:
                    row = connection.execute("SELECT * FROM memories WHERE kind=? AND digest=?",
                                             (kind, digest)).fetchone()
                policy = load_policy if load_policy is not None else (row["load_policy"] if row else "on_demand")
                importance = priority if priority is not None else (row["priority"] if row else 50)
                if policy == "always" and (kind == "episodic" or len(content) > 500):
                    raise ValueError("always memory must be semantic/procedural and at most 500 characters")
                # 同键更新和正文去重都不能绕过常驻条目的用户来源检查。
                if (policy == "always" or (row and row["load_policy"] == "always")) and not allow_always:
                    raise ValueError("changing always memory requires evidence quoted from the current user's request")
                identifier = row["id"] if row else "mem_" + uuid.uuid4().hex[:16]
                # 检查在事务内完成，避免校验后其他写入改变匹配到的条目。
                if policy == "always" and core_budget is not None:
                    if core_tokens(identifier, row["key"] if row else key, content) > core_budget:
                        if row and row["load_policy"] == "always":
                            raise ValueError("core budget exceeded; existing always memory was not changed")
                        policy = "on_demand"
                now = time.time()
                if row is not None:
                    connection.execute("UPDATE memories SET content=?,tags=?,source=?,updated_at=?,digest=?,load_policy=?,priority=? WHERE id=?",
                                       (content, json.dumps(tags, ensure_ascii=False), source, now, digest,
                                        policy, importance, row["id"]))
                    identifier = row["id"]
                else:
                    count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                    if count >= self.max_entries:
                        oldest = connection.execute("SELECT id FROM memories WHERE kind='episodic' ORDER BY updated_at LIMIT 1").fetchone()
                        if oldest is None:
                            raise ValueError("long-term memory is full; forget an entry before adding another")
                        connection.execute("DELETE FROM memories WHERE id=?", (oldest[0],))
                    connection.execute("INSERT INTO memories (id,kind,key,content,tags,source,created_at,updated_at,digest,load_policy,priority) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                       (identifier, kind, key, content, json.dumps(tags, ensure_ascii=False),
                                        source, now, now, digest, policy, importance))
                saved = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
            return self._entry(saved)
        finally:
            connection.close()

    def search(self, query: str = "", *, kind: str | None = None, limit: int = 10,
               recall: bool = False, load_policy: str | None = None) -> list[MemoryEntry]:
        """普通知识按关键词重叠排序；常驻资料由 load_always 单独加载。"""
        if not isinstance(query, str) or len(query) > 8000:
            raise ValueError("query must be text of at most 8000 characters")
        if kind is not None and kind not in MEMORY_KINDS:
            raise ValueError("invalid memory kind")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        if load_policy is not None and load_policy not in LOAD_POLICIES:
            raise ValueError("load_policy must be always or on_demand")
        if not self.path.exists():
            return []
        connection = self._connect()
        try:
            clauses, parameters = [], []
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
            score = len(terms & _terms(entry.key + " " + entry.content + " " + " ".join(entry.tags)))
            if query.strip() and not score:
                continue
            ranked.append((score, entry.updated_at, entry))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        # 排序后再处理少量返回条目，既保护旧库的读取出口，也不重复扫描整库。
        return [_redacted_entry(entry) for _, _, entry in ranked[:limit]]

    def load_always(self, limit: int = 8) -> tuple[list[MemoryEntry], int]:
        """按优先级、更新时间读取常驻条目，不参与关键词筛选或普通 top_k 竞争。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("always limit must be 1..100")
        if not self.path.exists():
            return [], 0
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM memories WHERE load_policy='always'").fetchone()[0]
            rows = connection.execute("SELECT * FROM memories WHERE load_policy='always' "
                                      "ORDER BY priority DESC, updated_at DESC, id LIMIT ?", (limit,)).fetchall()
            return [_redacted_entry(self._entry(row)) for row in rows], total
        finally:
            connection.close()

    def forget(self, identifier: str) -> bool:
        """删除长期库中的条目；会话、短期摘要和后台归档不在此接口的删除范围。"""
        if not isinstance(identifier, str) or not re.fullmatch(r"mem_[0-9a-f]{16}", identifier):
            raise ValueError("invalid memory id")
        if not self.path.exists():
            return False
        connection = self._connect()
        try:
            with connection:
                return connection.execute("DELETE FROM memories WHERE id=?", (identifier,)).rowcount > 0
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
                  "activated_as_always": entry.load_policy == "always"}
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
            value: dict[str, Any] = core_value(entry.id, entry.key, entry.content)
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
        self.store.remember("episodic", episode_key, content,
                            sorted({r.name for r in results if r.name not in MEMORY_TOOLS})[:8],
                            source="tool observations:" + source)

    def execute(self, name: str, arguments: dict[str, Any], maximum: int) -> ToolResult:
        """模型只能操作当前项目的条目，不能提交宿主路径或修改存储位置。"""
        try:
            if maximum < 160:
                raise ValueError("max_tool_output_chars must be at least 160 for memory tools")
            if name == "memory_search":
                allowed = {"query", "kind", "limit", "load_policy"}
                if set(arguments) - allowed:
                    raise ValueError("unexpected memory argument")
                entries = self.store.search(arguments.get("query", ""),
                                            kind=arguments.get("kind"), limit=arguments.get("limit", 10),
                                            load_policy=arguments.get("load_policy"))
                values: Any = {"memories": [asdict(entry) for entry in entries], "omitted": 0}
            elif name == "memory_remember":
                if set(arguments) - {"kind", "key", "content", "tags", "load_policy", "priority", "evidence"}:
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
                    priority=arguments.get("priority"), allow_always=verified)
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
                               ("id", "saved", "load_policy", "activated_as_always", "reason")
                               if key in values}
                    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > maximum:
                raise ValueError("max_tool_output_chars is too small for memory metadata")
            return ToolResult("", name, encoded)
        except (ValueError, OSError, sqlite3.Error) as exc:
            return ToolResult("", name, f"Error: {exc}", True)
