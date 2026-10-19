"""当前项目记忆的事务建库与有界版本日志。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

SCHEMA_VERSION = 3
MAX_VERSIONS = 20
MAX_EVENTS = 5_000


def initialize_schema(connection: sqlite3.Connection, workspace: str) -> None:
    """事务创建新库；已有数据库只接受当前 schema，不迁移或清理旧库。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("memory database schema is newer than this application")
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise ValueError(f"unsupported memory database schema {version}; only schema {SCHEMA_VERSION} is supported")
    with connection:
        # 并发打开时只有一个连接建库；其他连接获得锁后重新检查版本。
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("memory database schema is newer than this application")
        if version == SCHEMA_VERSION:
            return
        tables = connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if version != 0 or tables:
            raise ValueError(f"unsupported memory database schema {version}; only a new database or schema {SCHEMA_VERSION} is supported")
        # 不使用 executescript：DDL 和 user_version 必须一起提交，失败后可重试。
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO metadata VALUES ('workspace', ?)", (workspace,))
        connection.execute("INSERT INTO metadata VALUES ('revision','0')")
        connection.execute("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
            content TEXT NOT NULL, tags TEXT NOT NULL, source TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, digest TEXT NOT NULL,
            load_policy TEXT NOT NULL DEFAULT 'on_demand', priority INTEGER NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
            supersedes_revision INTEGER, source_kind TEXT NOT NULL DEFAULT 'user_action',
            source_refs TEXT NOT NULL DEFAULT '[]', recall_text TEXT NOT NULL DEFAULT '',
            scope_type TEXT NOT NULL DEFAULT 'workspace', scope_id TEXT NOT NULL DEFAULT '',
            verification_status TEXT NOT NULL DEFAULT 'unknown', verified_at REAL,
            conditions TEXT NOT NULL DEFAULT '{}', expires_at REAL,
            content_fingerprint TEXT NOT NULL DEFAULT '', fingerprint_version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(kind,key))""")
        for statement in (
            "CREATE INDEX memories_loading ON memories(load_policy,priority,updated_at)",
            "CREATE INDEX memories_digest ON memories(kind,digest)",
            "CREATE INDEX memories_state ON memories(status,load_policy)",
            "CREATE TABLE memory_versions (memory_id TEXT, revision INTEGER, snapshot TEXT NOT NULL, PRIMARY KEY(memory_id,revision))",
            "CREATE TABLE memory_events (sequence INTEGER PRIMARY KEY, memory_id TEXT, action TEXT NOT NULL, created_at REAL NOT NULL)",
        ):
            connection.execute(statement)
        connection.execute("INSERT INTO memory_events(memory_id,action,created_at) VALUES(NULL,?,?)",
                           ("schema:create", time.time()))
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def fingerprint(kind: str, content: str) -> str:
    """按已知情景格式去掉追踪编号；保留大小写和否定，避免错误合并事实。"""
    if kind == "episodic":
        content = re.sub(r"\s*\(call_id=[^\n)]*\)", "", content)
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()


def record_change(connection: sqlite3.Connection, identifier: str, action: str) -> None:
    """保存当前版本并增加库修订号；版本和事件不占逻辑条目的配额。"""
    row = connection.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone()
    if row is not None and row["status"] != "deleted":
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
