"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


class Tx:
    """单个事务内的记录读写，供服务层编排多记录原子操作。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(self, kind: str, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            cursor = self.connection.execute(
                "INSERT INTO records(kind,reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (kind, reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        record_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "created", actor_id, 1, json.dumps({"kind": kind, "state": state}, ensure_ascii=False, sort_keys=True), now),
        )
        return self.get(record_id)

    def get(self, record_id: int) -> Dict[str, Any]:
        row = self.connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return _row_to_dict(row)

    def find_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()
        return _row_to_dict(row) if row else None

    def list_records(self, kind: Optional[str] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM records"
        clauses, params = [], []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if state:
            clauses.append("state=?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        row = self.connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        self.connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
        )
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        return self.get(record_id)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        row = self.connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        rows = self.connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result


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

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_kind_state ON records(kind, state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        """BEGIN IMMEDIATE 保证并发调用串行化，异常自动回滚。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield Tx(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, record_id: int) -> Dict[str, Any]:
        with self.transaction() as tx:
            return tx.get(record_id)

    def find_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self.transaction() as tx:
            return tx.find_by_reference(reference)

    def list_records(self, kind: Optional[str] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self.transaction() as tx:
            return tx.list_records(kind=kind, state=state, limit=limit)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self.transaction() as tx:
            tx.add_audit(record_id, actor_id, action, details)

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        with self.transaction() as tx:
            return tx.audit_timeline(record_id)

    def stats(self) -> Dict[str, Dict[str, int]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT kind, state, COUNT(*) AS total FROM records GROUP BY kind, state").fetchall()
        result: Dict[str, Dict[str, int]] = {}
        for row in rows:
            result.setdefault(str(row["kind"]), {})[str(row["state"])] = int(row["total"])
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
