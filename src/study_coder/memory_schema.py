"""项目记忆的事务迁移与有界版本日志；迁移不请求模型、不改变旧正文时间。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = 2
MAX_VERSIONS = 20
MAX_EVENTS = 5_000
MAX_TOMBSTONES = 100_000
MAX_ORIGINS = 100_000


def _backup(path: Path, version: int) -> None:
    """持有源库写预约锁时，用另一个只读事务备份已提交状态。

    backup 不在持锁连接本身运行，避免 SQLite 等待自己的写事务。
    临时库关闭后才发布；每个旧版本只保留一份迁移前备份。
    """
    target = path.with_name(f"memory.schema-{version}.backup.sqlite3")
    if target.exists():
        return
    temporary = path.with_name(f"migration-{uuid.uuid4().hex}.sqlite3")
    try:
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(temporary)) as destination:
            source.backup(destination)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def migrate(connection: sqlite3.Connection, path: Path, workspace: str) -> None:
    """在一个事务内迁移 0/1 版，未来版本和归属不匹配的库保持原样。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("memory database schema is newer than this application")
    if version == SCHEMA_VERSION:
        return
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("memory database schema is newer than this application")
        if version == SCHEMA_VERSION:
            return
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "metadata" in tables:
            owner = connection.execute("SELECT value FROM metadata WHERE key='workspace'").fetchone()
            if owner and owner[0] != workspace:
                raise ValueError("memory workspace does not match")
        if "memories" in tables:
            _backup(path, version)
        # 不使用 executescript：它会提前提交，破坏迁移的事务边界。
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('workspace', ?)", (workspace,))
        connection.execute("""CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
            content TEXT NOT NULL, tags TEXT NOT NULL, source TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, digest TEXT NOT NULL,
            UNIQUE(kind,key))""")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
        additions = {
            "load_policy": "TEXT NOT NULL DEFAULT 'on_demand'",
            "priority": "INTEGER NOT NULL DEFAULT 50",
            "status": "TEXT NOT NULL DEFAULT 'active'",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "supersedes_revision": "INTEGER",
            "source_kind": "TEXT NOT NULL DEFAULT 'legacy'",
            "source_refs": "TEXT NOT NULL DEFAULT '[]'",
            "recall_text": "TEXT NOT NULL DEFAULT ''",
            "scope_type": "TEXT NOT NULL DEFAULT 'workspace'",
            "scope_id": "TEXT NOT NULL DEFAULT ''",
            "verification_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "verified_at": "REAL",
            "conditions": "TEXT NOT NULL DEFAULT '{}'",
            "expires_at": "REAL",
            "content_fingerprint": "TEXT NOT NULL DEFAULT ''",
            "fingerprint_version": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE memories ADD COLUMN {name} {declaration}")
        connection.execute("UPDATE memories SET recall_text=content WHERE recall_text=''")
        connection.execute("UPDATE memories SET scope_id=? WHERE scope_id=''", (workspace,))
        for row in connection.execute("SELECT id,kind,content FROM memories WHERE content_fingerprint=''").fetchall():
            connection.execute("UPDATE memories SET content_fingerprint=? WHERE id=?",
                               (fingerprint(row['kind'], row['content']), row['id']))
        for statement in (
            "CREATE INDEX IF NOT EXISTS memories_loading ON memories(load_policy,priority,updated_at)",
            "CREATE INDEX IF NOT EXISTS memories_digest ON memories(kind,digest)",
            "CREATE INDEX IF NOT EXISTS memories_state ON memories(status,load_policy)",
            "CREATE TABLE IF NOT EXISTS memory_versions (memory_id TEXT, revision INTEGER, snapshot TEXT NOT NULL, PRIMARY KEY(memory_id,revision))",
            "CREATE TABLE IF NOT EXISTS memory_tombstones (token TEXT PRIMARY KEY, memory_id TEXT NOT NULL, created_at REAL NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory_origins (memory_id TEXT, token TEXT, PRIMARY KEY(memory_id,token))",
            "CREATE TABLE IF NOT EXISTS memory_events (sequence INTEGER PRIMARY KEY, memory_id TEXT, action TEXT NOT NULL, created_at REAL NOT NULL)",
        ):
            connection.execute(statement)
        for row in connection.execute("SELECT * FROM memories").fetchall():
            connection.execute("INSERT OR IGNORE INTO memory_versions VALUES (?,?,?)",
                               (row["id"], row["revision"], json.dumps(dict(row), ensure_ascii=False)))
            _record_origins(connection, row)
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('revision','0')")
        connection.execute("INSERT INTO memory_events(memory_id,action,created_at) VALUES(NULL,?,?)",
                           (f"migrate:{version}->{SCHEMA_VERSION}", time.time()))
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def fingerprint(kind: str, content: str) -> str:
    """按已知情景格式去掉追踪编号；保留大小写和否定，避免错误合并事实。"""
    if kind == "episodic":
        content = re.sub(r"\s*\(call_id=[^\n)]*\)", "", content)
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()


def identity(workspace: str, kind: str, category: str, value: str) -> str:
    return hashlib.sha256(json.dumps([workspace, kind, category, value]).encode()).hexdigest()


def _record_origins(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    """版本正文可裁减，撤销身份需覆盖被裁减的旧版本，避免从旧会话重建。"""
    tokens = {identity(row["scope_id"], row["kind"], "key", row["key"]),
              identity(row["scope_id"], row["kind"], "fact", row["content_fingerprint"])}
    tokens.update(identity(row["scope_id"], row["kind"], "event", ref["event_id"])
                  for ref in json.loads(row["source_refs"]))
    existing = {item[0] for item in connection.execute("SELECT token FROM memory_origins WHERE memory_id=?", (row["id"],))}
    new = tokens - existing
    if connection.execute("SELECT COUNT(*) FROM memory_origins").fetchone()[0] + len(new) > MAX_ORIGINS:
        raise ValueError("memory source identity capacity exceeded; no data was changed")
    connection.executemany("INSERT INTO memory_origins VALUES (?,?)", [(row["id"], token) for token in new])


def record_change(connection: sqlite3.Connection, identifier: str, action: str) -> None:
    """保存当前版本并增加库修订号；版本和事件不占逻辑条目的配额。"""
    row = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
    if row is not None and row["status"] != "deleted":
        _record_origins(connection, row)
        connection.execute("INSERT OR REPLACE INTO memory_versions VALUES (?,?,?)",
                           (identifier, row["revision"], json.dumps(dict(row), ensure_ascii=False)))
        connection.execute("""DELETE FROM memory_versions WHERE memory_id=? AND revision NOT IN (
            SELECT revision FROM memory_versions WHERE memory_id=? ORDER BY revision DESC LIMIT ?)""",
                           (identifier, identifier, MAX_VERSIONS))
    connection.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
    connection.execute("INSERT INTO memory_events(memory_id,action,created_at) VALUES (?,?,?)",
                       (identifier, action, time.time()))
    connection.execute("DELETE FROM memory_events WHERE sequence NOT IN (SELECT sequence FROM memory_events ORDER BY sequence DESC LIMIT ?)",
                       (MAX_EVENTS,))
