"""SQLite 许可台账存储层。

所有写操作经由事务提交；审计事件表仅追加，不允许更新或删除，
保证已完成的测试记录与历史决策不被篡改。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sequences (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS enterprises (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    credit_code TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
    id                       TEXT PRIMARY KEY,
    enterprise_id            TEXT NOT NULL REFERENCES enterprises(id),
    plate                    TEXT NOT NULL UNIQUE,
    qualification_expires_at TEXT NOT NULL,
    status                   TEXT NOT NULL,
    created_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drivers (
    id                       TEXT PRIMARY KEY,
    enterprise_id            TEXT NOT NULL REFERENCES enterprises(id),
    name                     TEXT NOT NULL,
    license_no               TEXT NOT NULL UNIQUE,
    authorization_expires_at TEXT NOT NULL,
    status                   TEXT NOT NULL,
    created_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id            TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    vehicle_id    TEXT NOT NULL REFERENCES vehicles(id),
    driver_id     TEXT NOT NULL REFERENCES drivers(id),
    segment_id    TEXT NOT NULL REFERENCES segments(id),
    valid_from    TEXT NOT NULL,
    valid_to      TEXT NOT NULL,
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_applications_scope
    ON applications(enterprise_id, vehicle_id, segment_id, status);

CREATE TABLE IF NOT EXISTS approvals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications(id),
    level          INTEGER NOT NULL,
    approver       TEXT NOT NULL,
    decision       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permits (
    id             TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    enterprise_id  TEXT NOT NULL,
    vehicle_id     TEXT NOT NULL,
    driver_id      TEXT NOT NULL,
    segment_id     TEXT NOT NULL,
    version        INTEGER NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT NOT NULL,
    window_start   TEXT NOT NULL,
    window_end     TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(vehicle_id, segment_id, version)
);
CREATE INDEX IF NOT EXISTS idx_permits_scope ON permits(vehicle_id, segment_id, status);

CREATE TABLE IF NOT EXISTS trips (
    id                TEXT PRIMARY KEY,
    permit_id         TEXT NOT NULL REFERENCES permits(id),
    enterprise_id     TEXT NOT NULL,
    vehicle_id        TEXT NOT NULL,
    substitute_for    TEXT,
    driver_id         TEXT NOT NULL,
    segment_id        TEXT NOT NULL,
    planned_start     TEXT NOT NULL,
    planned_end       TEXT NOT NULL,
    status            TEXT NOT NULL,
    decision          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    completed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trips_vehicle ON trips(vehicle_id, status);
CREATE INDEX IF NOT EXISTS idx_trips_permit  ON trips(permit_id, status);

CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    vehicle_id  TEXT NOT NULL REFERENCES vehicles(id),
    trip_id     TEXT REFERENCES trips(id),
    description TEXT NOT NULL,
    status      TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
"""

ID_PREFIXES = {
    "enterprise": "ENT",
    "vehicle": "VEH",
    "driver": "DRV",
    "segment": "SEG",
    "application": "APP",
    "permit": "PER",
    "trip": "TRP",
    "incident": "INC",
}


class Ledger:
    """对 SQLite 台账的线程安全封装。服务重启后重新打开同一文件即可恢复。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """写事务：业务操作与审计事件在同一事务内提交或回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def next_id(self, kind: str) -> str:
        prefix = ID_PREFIXES[kind]
        row = self._conn.execute(
            "SELECT value FROM sequences WHERE name = ?", (prefix,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO sequences(name, value) VALUES (?, 1)", (prefix,)
            )
            value = 1
        else:
            value = row["value"] + 1
            self._conn.execute(
                "UPDATE sequences SET value = ? WHERE name = ?", (value, prefix)
            )
        return f"{prefix}-{value:04d}"

    def insert(self, table: str, row: dict[str, Any]) -> None:
        columns = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        self._conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", row
        )

    def update(self, table: str, entity_id: str, changes: dict[str, Any]) -> int:
        assignments = ", ".join(f"{k} = :{k}" for k in changes)
        cur = self._conn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = :__id",
            {**changes, "__id": entity_id},
        )
        return cur.rowcount

    def get(self, table: str, entity_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (entity_id,)
            ).fetchone()
        return dict(row) if row else None

    def find(
        self,
        table: str,
        where: str = "1=1",
        params: tuple[Any, ...] = (),
        order: str = "rowid",
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", params
            ).fetchall()
        return [dict(r) for r in rows]

    def append_event(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        reason: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        import json

        self._conn.execute(
            "INSERT INTO events(entity_type, entity_id, action, actor, reason,"
            " payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entity_type,
                entity_id,
                action,
                actor,
                reason,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
