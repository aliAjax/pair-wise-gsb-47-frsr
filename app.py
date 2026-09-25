"""应用入口：参数解析、依赖组装与HTTP服务生命周期。"""
import argparse
import os
from pathlib import Path

from src.audit import AuditRecorder
from src.http_api import create_server
from src.repository import Repository
from src.rules import BloodRules
from src.service import HOLD_TTL_SECONDS, Service


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = os.environ.get("BLOOD_DESK_DB", str(BASE_DIR / "blood-desk.db"))
DEFAULT_PORT = int(os.environ.get("BLOOD_DESK_PORT", "8323"))
DEFAULT_HOLD_TTL = int(os.environ.get("BLOOD_DESK_HOLD_TTL", str(HOLD_TTL_SECONDS)))


def build_service(db_path: str = DEFAULT_DB, hold_ttl_seconds: int = DEFAULT_HOLD_TTL) -> Service:
    repository = Repository(db_path)
    audit = AuditRecorder(repository)
    return Service(repository, BloodRules(), audit, hold_ttl_seconds=hold_ttl_seconds)


def parse_args():
    parser = argparse.ArgumentParser(description="血液调剂台")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite数据库路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--hold-ttl", type=int, default=DEFAULT_HOLD_TTL, help="锁库超时秒数")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    service = build_service(args.db, hold_ttl_seconds=args.hold_ttl)
    server = create_server(args.host, args.port, service, BASE_DIR / "static")
    print("血液调剂台 listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
