"""SQLite 持久层：仅负责存取，业务规则在 service 层。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviewers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer_id TEXT NOT NULL,
    institution_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(reviewer_id, institution_id)
);
CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    deduction REAL NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    kind TEXT NOT NULL,
    reason TEXT,
    retroactive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(code, version)
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    period TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remediations (
    id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    submit_note TEXT,
    verified_at TEXT,
    effective_date TEXT,
    verifier_id TEXT,
    verify_note TEXT
);
CREATE TABLE IF NOT EXISTS appeals (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL,
    period TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decider_id TEXT,
    decision_note TEXT
);
CREATE TABLE IF NOT EXISTS supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL,
    note TEXT NOT NULL,
    attachments TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    name TEXT NOT NULL,
    cutoff_date TEXT NOT NULL,
    status TEXT NOT NULL,
    current_version INTEGER,
    next_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_versions (
    batch_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    reason TEXT,
    appeal_id TEXT,
    snapshot TEXT,
    digest TEXT,
    results TEXT,
    PRIMARY KEY(batch_id, version)
);
CREATE TABLE IF NOT EXISTS revocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    institution_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    draft_version INTEGER,
    params TEXT NOT NULL,
    results TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def _adapt(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


class Store:
    """轻量 SQLite 封装，行以 dict 返回，复杂字段用 JSON 列。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def query(self, sql: str, **params: Any) -> list[dict]:
        cur = self.conn.execute(sql, {k: _adapt(v) for k, v in params.items()})
        return [dict(row) for row in cur.fetchall()]

    def one(self, sql: str, **params: Any) -> dict | None:
        rows = self.query(sql, **params)
        return rows[0] if rows else None

    def get(self, sql: str, **params: Any) -> dict:
        row = self.one(sql, **params)
        if row is None:
            raise LookupError("未找到记录")
        return row

    def execute(self, sql: str, **params: Any) -> int:
        cur = self.conn.execute(sql, {k: _adapt(v) for k, v in params.items()})
        self.conn.commit()
        return cur.lastrowid
