"""调剂台账（事件流）封装，查询保持只读。"""
from typing import Any, Dict, List


class AuditRecorder:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def events(self, request_id: int) -> List[Dict[str, Any]]:
        with self.repository._connect() as conn:
            return self.repository.events_for(conn, request_id)

    def note(self, conn: Any, action: str, actor_id: str, details: Dict[str, Any], request_id: int = None) -> None:
        self.repository.add_event(conn, action, actor_id, details, request_id)
