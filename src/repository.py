"""SQLite 表结构、事务与持久化查询。

表：医院、血袋批次、伤员、用血请求、锁库明细、调剂台账（事件）。
库存扣减只发生在医院实发；锁库数量由 allocations 实时汇总，不落死字段。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .domain import Conflict, NotFound


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_text() -> str:
    return utc_now().isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def transaction(self):
        """写事务：BEGIN IMMEDIATE 立即取库级写锁，串行化并发抢占。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS hospitals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    distance_km REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
                    blood_type TEXT NOT NULL,
                    component TEXT NOT NULL,
                    expires_on TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS casualties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    casualty_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    blood_type TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_no TEXT NOT NULL UNIQUE,
                    casualty_id INTEGER NOT NULL REFERENCES casualties(id),
                    blood_type TEXT NOT NULL,
                    component TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    shipped_quantity INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    held_expires_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    shipped_at TEXT
                );
                CREATE TABLE IF NOT EXISTS allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    batch_id INTEGER NOT NULL REFERENCES batches(id),
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    shipped_at TEXT,
                    released_at TEXT,
                    release_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER REFERENCES requests(id),
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batches_lookup ON batches(component, blood_type);
                CREATE INDEX IF NOT EXISTS idx_requests_state ON requests(state, id);
                CREATE INDEX IF NOT EXISTS idx_alloc_batch ON allocations(batch_id, status);
                CREATE INDEX IF NOT EXISTS idx_alloc_request ON allocations(request_id);
                CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id, id);
                """
            )

    # ---- 通用 ----
    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    @staticmethod
    def _next_code(conn: sqlite3.Connection, prefix: str, table: str, column: str) -> str:
        row = conn.execute("SELECT %s AS code FROM %s WHERE %s LIKE ? ORDER BY id DESC LIMIT 1" % (column, table, column), (prefix + "%",)).fetchone()
        if row is None or not row["code"][len(prefix):].isdigit():
            return "%s1" % prefix
        return "%s%d" % (prefix, int(row["code"][len(prefix):]) + 1)

    @staticmethod
    def _event(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    def add_event(self, conn: sqlite3.Connection, action: str, actor_id: str, details: Dict[str, Any], request_id: Optional[int] = None) -> None:
        conn.execute(
            "INSERT INTO events(request_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
            (request_id, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), now_text()),
        )

    # ---- 医院 ----
    def create_hospital(self, conn: sqlite3.Connection, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        code = data.get("code") or self._next_code(conn, "H", "hospitals", "code")
        try:
            cursor = conn.execute(
                "INSERT INTO hospitals(code,name,distance_km,created_by,created_at) VALUES(?,?,?,?,?)",
                (code, data["name"], data["distance_km"], actor_id, now_text()),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("医院编号已存在") from exc
        return self.get_hospital(conn, int(cursor.lastrowid))

    def get_hospital(self, conn: sqlite3.Connection, hospital_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
        if row is None:
            raise NotFound("医院不存在")
        return dict(row)

    def list_hospitals(self, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        own = conn is None
        conn = conn or self._connect()
        try:
            rows = conn.execute("SELECT * FROM hospitals ORDER BY id").fetchall()
            return [dict(row) for row in rows]
        finally:
            if own:
                conn.close()

    # ---- 血袋批次 ----
    def create_batch(self, conn: sqlite3.Connection, hospital_id: int, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        self.get_hospital(conn, hospital_id)
        batch_no = data.get("batch_no") or self._next_code(conn, "B", "batches", "batch_no")
        try:
            cursor = conn.execute(
                "INSERT INTO batches(batch_no,hospital_id,blood_type,component,expires_on,quantity,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (batch_no, hospital_id, data["blood_type"], data["component"], data["expires_on"],
                 data["quantity"], actor_id, now_text()),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("血袋批次号已存在") from exc
        return self.get_batch(conn, int(cursor.lastrowid))

    def get_batch(self, conn: sqlite3.Connection, batch_id: int) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT b.*, h.name AS hospital_name, h.distance_km AS distance_km FROM batches b"
            " JOIN hospitals h ON h.id = b.hospital_id WHERE b.id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise NotFound("血袋批次不存在")
        return dict(row)

    def list_batches(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT b.*, h.name AS hospital_name, h.distance_km AS distance_km FROM batches b"
            " JOIN hospitals h ON h.id = b.hospital_id ORDER BY b.expires_on, h.distance_km, b.id"
        ).fetchall()
        return [dict(row) for row in rows]

    def deduct_batch(self, conn: sqlite3.Connection, batch_id: int, quantity: int) -> None:
        conn.execute("UPDATE batches SET quantity = quantity - ? WHERE id=?", (quantity, batch_id))

    # ---- 伤员 ----
    def create_casualty(self, conn: sqlite3.Connection, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        number = data.get("casualty_no") or self._next_code(conn, "C", "casualties", "casualty_no")
        try:
            cursor = conn.execute(
                "INSERT INTO casualties(casualty_no,name,blood_type,created_by,created_at) VALUES(?,?,?,?,?)",
                (number, data["name"], data["blood_type"], actor_id, now_text()),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("伤员编号已存在") from exc
        return self.get_casualty(conn, int(cursor.lastrowid))

    def get_casualty(self, conn: sqlite3.Connection, casualty_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM casualties WHERE id=?", (casualty_id,)).fetchone()
        if row is None:
            raise NotFound("伤员不存在")
        return dict(row)

    def list_casualties(self, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        own = conn is None
        conn = conn or self._connect()
        try:
            rows = conn.execute("SELECT * FROM casualties ORDER BY id").fetchall()
            return [dict(row) for row in rows]
        finally:
            if own:
                conn.close()

    # ---- 用血请求 ----
    def create_request(self, conn: sqlite3.Connection, data: Dict[str, Any], actor_id: str) -> int:
        number = data.get("request_no") or self._next_code(conn, "R", "requests", "request_no")
        now = now_text()
        try:
            cursor = conn.execute(
                "INSERT INTO requests(request_no,casualty_id,blood_type,component,quantity,state,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (number, data["casualty_id"], data["blood_type"], data["component"],
                 data["quantity"], data["state"], actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("请求编号已存在") from exc
        return int(cursor.lastrowid)

    def get_request(self, conn: sqlite3.Connection, request_id: int) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT r.*, c.casualty_no AS casualty_no, c.name AS casualty_name FROM requests r"
            " JOIN casualties c ON c.id = r.casualty_id WHERE r.id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise NotFound("用血请求不存在")
        return dict(row)

    def list_requests(self, conn: sqlite3.Connection, state: Optional[str] = None) -> List[Dict[str, Any]]:
        if state:
            rows = conn.execute(
                "SELECT r.*, c.casualty_no AS casualty_no FROM requests r JOIN casualties c ON c.id=r.casualty_id"
                " WHERE r.state=? ORDER BY r.id",
                (state,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.*, c.casualty_no AS casualty_no FROM requests r JOIN casualties c ON c.id=r.casualty_id"
                " ORDER BY r.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_requests(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM requests WHERE state='pending' ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def held_requests(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM requests WHERE state='held' ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def update_request_state(
        self,
        conn: sqlite3.Connection,
        request_id: int,
        state: str,
        held_expires_at: Optional[str] = None,
        shipped_quantity: Optional[int] = None,
    ) -> None:
        if shipped_quantity is None:
            conn.execute(
                "UPDATE requests SET state=?, held_expires_at=?, updated_at=? WHERE id=?",
                (state, held_expires_at, now_text(), request_id),
            )
        else:
            conn.execute(
                "UPDATE requests SET state=?, held_expires_at=?, shipped_quantity=?, shipped_at=?, updated_at=? WHERE id=?",
                (state, held_expires_at, shipped_quantity, now_text() if state == "shipped" else None,
                 now_text(), request_id),
            )

    # ---- 锁库明细 ----
    def hold(self, conn: sqlite3.Connection, request_id: int, batch_id: int, quantity: int) -> None:
        conn.execute(
            "INSERT INTO allocations(request_id,batch_id,quantity,status,created_at) VALUES(?,?,?,?,?)",
            (request_id, batch_id, quantity, "held", now_text()),
        )

    def allocations_for(self, conn: sqlite3.Connection, request_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = (
            "SELECT a.*, b.batch_no, b.blood_type, b.component, b.expires_on, h.id AS hospital_id,"
            " h.name AS hospital_name, h.distance_km FROM allocations a"
            " JOIN batches b ON b.id=a.batch_id JOIN hospitals h ON h.id=b.hospital_id WHERE a.request_id=?"
        )
        params: List[Any] = [request_id]
        if status:
            sql += " AND a.status=?"
            params.append(status)
        sql += " ORDER BY a.id"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def held_quantities_by_batch(self, conn: sqlite3.Connection) -> Dict[int, int]:
        rows = conn.execute(
            "SELECT batch_id, SUM(quantity) AS total FROM allocations WHERE status='held' GROUP BY batch_id"
        ).fetchall()
        return {int(row["batch_id"]): int(row["total"]) for row in rows}

    def release_holds(self, conn: sqlite3.Connection, request_id: int, reason: str) -> None:
        conn.execute(
            "UPDATE allocations SET status='released', released_at=?, release_reason=? WHERE request_id=? AND status='held'",
            (now_text(), reason, request_id),
        )

    def mark_shipped(self, conn: sqlite3.Connection, allocation_id: int, quantity: int) -> None:
        conn.execute(
            "UPDATE allocations SET status='shipped', shipped_at=?, quantity=? WHERE id=?",
            (now_text(), quantity, allocation_id),
        )

    # ---- 台账/溯源 ----
    def events_for(self, conn: sqlite3.Connection, request_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM events WHERE request_id=? ORDER BY id", (request_id,)).fetchall()
        return [self._event(row) for row in rows]

    def trace_casualty(self, conn: sqlite3.Connection, casualty_id: int) -> Dict[str, Any]:
        casualty = self.get_casualty(conn, casualty_id)
        requests = self.list_requests(conn)
        result: Dict[str, Any] = dict(casualty)
        result["requests"] = []
        for request in requests:
            if int(request["casualty_id"]) != casualty_id:
                continue
            item = dict(request)
            item["allocations"] = self.allocations_for(conn, int(request["id"]))
            item["events"] = self.events_for(conn, int(request["id"]))
            result["requests"].append(item)
        return result

    # ---- 视图 ----
    def inventory_view(self, conn: sqlite3.Connection, today: str) -> List[Dict[str, Any]]:
        """每个批次的在库、已占用、可调剂、是否过期。"""
        rows = conn.execute(
            "SELECT b.id, b.batch_no, b.blood_type, b.component, b.expires_on, b.quantity,"
            " h.id AS hospital_id, h.name AS hospital_name, h.distance_km,"
            " COALESCE((SELECT SUM(quantity) FROM allocations a WHERE a.batch_id=b.id AND a.status='held'),0) AS held_qty"
            " FROM batches b JOIN hospitals h ON h.id=b.hospital_id ORDER BY b.id"
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["held_qty"] = int(item["held_qty"])
            item["available_qty"] = int(item["quantity"]) - item["held_qty"]
            item["expired"] = item["expires_on"] < today
            items.append(item)
        return items
