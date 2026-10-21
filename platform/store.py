"""
数据层 — SQLite。平台和脚本的分界线就在这里：抢过的每一次都留下可查的记录。

四张核心表：
    devices       设备台账（serial / 型号 / 归属账号 / 状态 / 能力标签）
    tasks         任务（场景 + 参数 + 开售时刻 + 状态 + 结果）
    assignments   任务↔设备的派发记录（一任务多机的核心；谁先成功谁赢）
    events        逐动作事件流（看板实时读这张表）
    results       最终结果（成功/失败/原因），一次任务可能多台各有一条

为什么不用 ORM：这套东西要在能跑 adb 的任意机器上跑起来，装依赖是最大的摩擦。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS devices (
    serial      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'adb',       -- adb | local | mock
    adb_path    TEXT DEFAULT '',
    endpoint    TEXT DEFAULT '',                    -- local 执行器的 http 地址
    model       TEXT DEFAULT '',
    android     TEXT DEFAULT '',
    account     TEXT DEFAULT '',                    -- 这台机登的哪个大麦号（人看得懂的备注）
    tags        TEXT DEFAULT '[]',                  -- ["damai","maoyan"]
    state       TEXT DEFAULT 'unknown',             -- idle | busy | offline | banned
    info        TEXT DEFAULT '{}',
    last_seen   REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    scene       TEXT NOT NULL DEFAULT 'damai',
    config      TEXT NOT NULL DEFAULT '{}',         -- 场景参数（DamaiConfig）
    sale_at     REAL NOT NULL,                      -- 开售时刻（unix，已按 NTP 校时）
    lead_ms     REAL DEFAULT 55,                    -- 提前多少毫秒发射
    device_filter TEXT DEFAULT '[]',                -- 参与设备的 tags/serial 过滤
    replicas    INTEGER DEFAULT 1,                  -- 派几台设备（FIRST_COMPLETED）
    priority    INTEGER DEFAULT 5,
    status      TEXT DEFAULT 'queued',              -- queued|armed|running|success|failed|cancelled
    outcome     TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    created_at  REAL DEFAULT 0,
    started_at  REAL DEFAULT 0,
    finished_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    serial      TEXT NOT NULL,
    assigned_at REAL DEFAULT 0,
    ended_at    REAL DEFAULT 0,
    outcome     TEXT DEFAULT '',                    -- success | failed | preempted | error
    detail      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_assign_task ON assignments(task_id);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    task_id TEXT NOT NULL,
    device  TEXT DEFAULT '',
    kind    TEXT DEFAULT '',
    state   TEXT DEFAULT '',
    action  TEXT DEFAULT '',
    ok      INTEGER DEFAULT 1,
    ms      REAL DEFAULT 0,
    detail  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);

CREATE TABLE IF NOT EXISTS results (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT NOT NULL,
    device   TEXT DEFAULT '',
    outcome  TEXT NOT NULL,                         -- success | failed
    order_id TEXT DEFAULT '',
    reason   TEXT DEFAULT '',
    ts       REAL DEFAULT 0
);
"""


@dataclass
class DeviceRow:
    serial: str
    kind: str = "adb"
    adb_path: str = ""
    endpoint: str = ""
    model: str = ""
    android: str = ""
    account: str = ""
    tags: list[str] | None = None
    state: str = "unknown"
    info: dict | None = None
    last_seen: float = 0.0

    def matches(self, filt: list[str]) -> bool:
        if not filt:
            return True
        tags = set(self.tags or [])
        return any(f == self.serial or f in tags or f == "*" for f in filt)


@dataclass
class TaskRow:
    id: str
    scene: str
    config: dict
    sale_at: float
    lead_ms: float = 55.0
    device_filter: list[str] | None = None
    replicas: int = 1
    priority: int = 5
    status: str = "queued"
    outcome: str = ""
    detail: str = ""
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0


class Store:
    """线程安全的 SQLite 封装。多线程共享一个连接 + 一把锁（写入量很小，够用）。"""

    def __init__(self, path: Path | str = "var/ticket.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ---------------- devices ----------------

    def upsert_device(self, d: DeviceRow) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO devices(serial,kind,adb_path,endpoint,model,android,account,tags,state,info,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(serial) DO UPDATE SET
                     kind=excluded.kind, adb_path=excluded.adb_path, endpoint=excluded.endpoint,
                     model=excluded.model, android=excluded.android, account=excluded.account,
                     tags=excluded.tags, state=excluded.state, info=excluded.info,
                     last_seen=excluded.last_seen""",
                (
                    d.serial, d.kind, d.adb_path, d.endpoint, d.model, d.android, d.account,
                    json.dumps(d.tags or [], ensure_ascii=False), d.state,
                    json.dumps(d.info or {}, ensure_ascii=False), d.last_seen or time.time(),
                ),
            )
            self._db.commit()

    def set_device_state(self, serial: str, state: str) -> None:
        with self._lock:
            self._db.execute("UPDATE devices SET state=?, last_seen=? WHERE serial=?", (state, time.time(), serial))
            self._db.commit()

    def devices(self) -> list[DeviceRow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM devices ORDER BY serial").fetchall()
        return [
            DeviceRow(
                serial=r["serial"], kind=r["kind"], adb_path=r["adb_path"], endpoint=r["endpoint"],
                model=r["model"], android=r["android"], account=r["account"],
                tags=json.loads(r["tags"] or "[]"), state=r["state"],
                info=json.loads(r["info"] or "{}"), last_seen=r["last_seen"],
            )
            for r in rows
        ]

    # ---------------- tasks ----------------

    def create_task(self, t: TaskRow) -> TaskRow:
        t.id = t.id or uuid.uuid4().hex[:12]
        t.created_at = t.created_at or time.time()
        with self._lock:
            self._db.execute(
                """INSERT INTO tasks(id,scene,config,sale_at,lead_ms,device_filter,replicas,priority,
                                     status,outcome,detail,created_at,started_at,finished_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t.id, t.scene, json.dumps(t.config, ensure_ascii=False), t.sale_at, t.lead_ms,
                    json.dumps(t.device_filter or [], ensure_ascii=False), t.replicas, t.priority,
                    t.status, t.outcome, t.detail, t.created_at, t.started_at, t.finished_at,
                ),
            )
            self._db.commit()
        return t

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
            self._db.commit()

    def task(self, task_id: str) -> TaskRow | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_from(r) if r else None

    def tasks(self, limit: int = 100, status: str | None = None) -> list[TaskRow]:
        q = "SELECT * FROM tasks"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._task_from(r) for r in rows]

    @staticmethod
    def _task_from(r: sqlite3.Row) -> TaskRow:
        return TaskRow(
            id=r["id"], scene=r["scene"], config=json.loads(r["config"] or "{}"),
            sale_at=r["sale_at"], lead_ms=r["lead_ms"],
            device_filter=json.loads(r["device_filter"] or "[]"), replicas=r["replicas"],
            priority=r["priority"], status=r["status"], outcome=r["outcome"], detail=r["detail"],
            created_at=r["created_at"], started_at=r["started_at"], finished_at=r["finished_at"],
        )

    # ---------------- assignments ----------------

    def assign(self, task_id: str, serial: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO assignments(task_id,serial,assigned_at) VALUES(?,?,?)",
                (task_id, serial, time.time()),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def end_assign(self, assign_id: int, outcome: str, detail: str = "") -> None:
        with self._lock:
            self._db.execute(
                "UPDATE assignments SET ended_at=?, outcome=?, detail=? WHERE id=?",
                (time.time(), outcome, detail, assign_id),
            )
            self._db.commit()

    def assignments(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM assignments WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- events ----------------

    def add_event(self, task_id: str, device: str, kind: str, state: str = "", action: str = "",
                  ok: bool = True, ms: float = 0, detail: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events(ts,task_id,device,kind,state,action,ok,ms,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), task_id, device, kind, state, action, int(ok), ms, detail),
            )
            self._db.commit()

    def events(self, task_id: str | None = None, since_id: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            if task_id:
                rows = self._db.execute(
                    "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                    (task_id, since_id, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (since_id, limit)
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- results ----------------

    def add_result(self, task_id: str, device: str, outcome: str, order_id: str = "", reason: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO results(task_id,device,outcome,order_id,reason,ts) VALUES(?,?,?,?,?,?)",
                (task_id, device, outcome, order_id, reason, time.time()),
            )
            self._db.commit()

    def results(self, task_id: str | None = None) -> list[dict]:
        with self._lock:
            if task_id:
                rows = self._db.execute("SELECT * FROM results WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM results ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(r) for r in rows]

    # ---------------- 统计 ----------------

    def stats(self) -> dict:
        with self._lock:
            d = self._db.execute("SELECT COUNT(*) c, SUM(state='idle') i FROM devices").fetchone()
            t = self._db.execute(
                "SELECT status, COUNT(*) c FROM tasks GROUP BY status"
            ).fetchall()
            r = self._db.execute(
                "SELECT outcome, COUNT(*) c FROM results GROUP BY outcome"
            ).fetchall()
        return {
            "devices": d["c"] or 0,
            "idle": d["i"] or 0,
            "tasks": {row["status"]: row["c"] for row in t},
            "results": {row["outcome"]: row["c"] for row in r},
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
