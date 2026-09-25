"""HTTP 路由与统一错误输出。

路由划分：
  /api/hospitals          医院登记/列表
  /api/batches            血袋登记；GET 库存视图
  /api/casualties         伤员登记/列表
  /api/requests           用血请求（提交即匹配）
  /api/requests/{id}/ship|cancel
  /api/casualties/{id}/trace  沿伤员查调剂去向
  /api/sweep              超时/过期清扫
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


REQUEST_ACTION_RE = re.compile(r"^/api/requests/(\d+)/(ship|cancel)$")
REQUEST_RE = re.compile(r"^/api/requests/(\d+)$")
CASUALTY_TRACE_RE = re.compile(r"^/api/casualties/(\d+)/trace$")
CASUALTY_RE = re.compile(r"^/api/casualties/(\d+)$")
HOSPITAL_BATCH_RE = re.compile(r"^/api/hospitals/(\d+)/batches$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "blood-desk/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "blood-desk", "database": service.health()})
                    return
                if parsed.path == "/":
                    self._send(200, (static_dir / "index.html").read_bytes(), "text/html; charset=utf-8")
                    return
                actor = self._actor()
                if parsed.path == "/api/hospitals":
                    self._send(200, {"items": service.list_hospitals(actor)})
                    return
                if parsed.path == "/api/batches":
                    self._send(200, {"items": service.list_batches(actor)})
                    return
                if parsed.path == "/api/casualties":
                    self._send(200, {"items": service.list_casualties(actor)})
                    return
                if parsed.path == "/api/requests":
                    query = parse_qs(parsed.query)
                    state = query.get("state", [None])[0]
                    self._send(200, {"items": service.list_requests(actor, state=state)})
                    return
                match = REQUEST_RE.match(parsed.path)
                if match:
                    self._send(200, service.request_detail(actor, int(match.group(1))))
                    return
                match = CASUALTY_TRACE_RE.match(parsed.path)
                if match:
                    self._send(200, service.trace_casualty(actor, int(match.group(1))))
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(actor))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                actor = self._actor()
                if parsed.path == "/api/hospitals":
                    self._send(201, service.register_hospital(actor, body))
                    return
                match = HOSPITAL_BATCH_RE.match(parsed.path)
                if match:
                    self._send(201, service.register_batch(actor, int(match.group(1)), body))
                    return
                if parsed.path == "/api/casualties":
                    self._send(201, service.register_casualty(actor, body))
                    return
                if parsed.path == "/api/requests":
                    self._send(201, service.create_request(actor, body))
                    return
                match = REQUEST_ACTION_RE.match(parsed.path)
                if match:
                    request_id, action = int(match.group(1)), match.group(2)
                    if action == "ship":
                        result = service.ship(actor, request_id, body)
                    else:
                        result = service.cancel(actor, request_id, body)
                    self._send(200, result)
                    return
                if parsed.path == "/api/sweep":
                    self._send(200, service.sweep(actor))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
