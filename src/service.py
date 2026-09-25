"""血液调剂台用例编排：登记、匹配锁库、实发扣库、取消/超时释放与排队晋升。

并发安全：所有会改动占用的用例都在单个 BEGIN IMMEDIATE 事务内完成，
读快照与写决策原子提交；HTTP 多线程与同进程多连接下后提交者看到的仍是旧占用。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError
from .repository import Repository, utc_now
from .rules import BloodRules, ST_HELD, ST_PENDING


ROLES = {"blood_bank", "dispatcher", "hospital", "admin"}
HOLD_TTL_SECONDS = 300


class Service:
    def __init__(
        self,
        repository: Repository,
        rules: BloodRules = None,
        audit: AuditRecorder = None,
        hold_ttl_seconds: int = HOLD_TTL_SECONDS,
    ) -> None:
        self.repository = repository
        self.rules = rules or BloodRules()
        self.audit = audit or AuditRecorder(repository)
        self.hold_ttl = timedelta(seconds=hold_ttl_seconds)

    # ---- 身份 ----
    @staticmethod
    def _actor(actor: Optional[Actor]) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _check_role(self, actor: Actor, allowed: set) -> None:
        if actor.role != "admin" and actor.role not in allowed:
            raise PermissionDenied("角色无权执行该操作")

    # ---- 时间 ----
    def _now(self) -> datetime:
        return utc_now()

    def _today(self, now: datetime) -> str:
        return now.date().isoformat()

    # ---- 登记 ----
    def register_hospital(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, {"blood_bank", "hospital"})
        data = self.rules.validate_hospital(payload or {})
        with self.repository.transaction() as conn:
            hospital = self.repository.create_hospital(conn, data, actor.user_id)
            self.repository.add_event(conn, "hospital_registered", actor.user_id, {"hospital": hospital})
        return hospital

    def register_batch(self, actor: Actor, hospital_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, {"blood_bank", "hospital"})
        data = self.rules.validate_batch(payload or {})
        if not self.rules.is_fresh(data["expires_on"], self._now().date()):
            raise ValidationError("过期血袋不能登记入库")
        with self.repository.transaction() as conn:
            batch = self.repository.create_batch(conn, hospital_id, data, actor.user_id)
            self.repository.add_event(conn, "batch_registered", actor.user_id, {"batch": batch})
            self._promote_pending(conn, actor)
        return self._batch_view(batch["id"])

    def register_casualty(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, {"dispatcher", "blood_bank"})
        from .rules import BLOOD_TYPES
        from .domain import text, choice
        p = dict(payload or {})
        name = text(p, "name")
        blood_type = choice(p, "blood_type", BLOOD_TYPES)
        data = {"name": name, "blood_type": blood_type, "casualty_no": p.get("casualty_no")}
        with self.repository.transaction() as conn:
            casualty = self.repository.create_casualty(conn, data, actor.user_id)
            self.repository.add_event(conn, "casualty_registered", actor.user_id, {"casualty": casualty})
        return casualty

    # ---- 请求与匹配 ----
    def create_request(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, {"dispatcher", "blood_bank"})
        data = self.rules.validate_request(payload or {})
        with self.repository.transaction() as conn:
            casualty = self.repository.get_casualty(conn, int(data["casualty_id"]))
            data["state"] = ST_PENDING
            request_id = self.repository.create_request(conn, data, actor.user_id)
            self.repository.add_event(
                conn, "request_created", actor.user_id,
                {"request_id": request_id, "casualty": casualty["casualty_no"], **data}, request_id,
            )
            self._sweep_locks(conn, actor)
            self._promote_pending(conn, actor)
            request = self.repository.get_request(conn, request_id)
        return self.request_detail(actor, request["id"])

    def _promote_pending(self, conn, actor: Actor) -> None:
        """按请求编号先后（FIFO）尝试把排队请求整体锁库。

        遇到第一个当前无法整体满足的请求即停止，不跳过老请求，避免插队。
        """
        now = self._now()
        batches = self.repository.list_batches(conn)
        held = self.repository.held_quantities_by_batch(conn)
        for request in self.repository.pending_requests(conn):
            plan, satisfied = self.rules.plan_allocation(request, batches, held, now.date())
            if satisfied < int(request["quantity"]):
                break
            expires_at = (now + self.hold_ttl).isoformat()
            for item in plan:
                self.repository.hold(conn, request["id"], item["batch_id"], item["quantity"])
                held[item["batch_id"]] = held.get(item["batch_id"], 0) + item["quantity"]
            self.repository.update_request_state(conn, request["id"], ST_HELD, expires_at)
            self.repository.add_event(
                conn, "held", actor.user_id,
                {"expires_at": expires_at, "plan": plan,
                 "note": "已按相容血型/效期/距离锁定，等待医院实发；后提交的同批请求留待确认"},
                request["id"],
            )

    # ---- 实发扣库 ----
    def ship(self, actor: Actor, request_id: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, {"hospital", "blood_bank"})
        overrides = self._ship_overrides(payload or {})
        with self.repository.transaction() as conn:
            request = self.repository.get_request(conn, request_id)
            if request["state"] != ST_HELD:
                raise Conflict("仅已锁库(held)的请求可以实发，当前状态：%s" % request["state"])
            holds = self.repository.allocations_for(conn, request_id, "held")
            now = self._now()
            # 锁库后若有批次过期，不能实发，释放并重新排队
            expired = [h for h in holds if not self.rules.is_fresh(h["expires_on"], now.date())]
            if expired:
                self._release_and_requeue(conn, request, "批次过期", actor)
                self._promote_pending(conn, actor)
                raise Conflict("锁定的血袋已过期，已释放并重新排队")
            if self.rules.hold_expired(request["held_expires_at"], now):
                self._release_and_requeue(conn, request, "锁库超时", actor)
                self._promote_pending(conn, actor)
                raise Conflict("锁库已超时，占用已释放并重新排队")
            shipped_total = 0
            shipped_lines = []
            for hold in holds:
                qty = overrides.get(int(hold["id"]), int(hold["quantity"]))
                if not isinstance(qty, int) or qty < 0 or qty > int(hold["quantity"]):
                    raise ValidationError("实发数量超出锁库范围")
                if qty == 0:
                    continue
                self.repository.mark_shipped(conn, hold["id"], qty)
                self.repository.deduct_batch(conn, hold["batch_id"], qty)
                shipped_total += qty
                shipped_lines.append({
                    "allocation_id": hold["id"], "batch_no": hold["batch_no"],
                    "hospital": hold["hospital_name"], "quantity": qty,
                })
            if shipped_total == 0:
                raise ValidationError("实发数量不能全部为0")
            self.repository.update_request_state(conn, request_id, "shipped", None, shipped_total)
            # 未实发的余量回到池子
            self.repository.release_holds(conn, request_id, "实发余量释放")
            self.repository.add_event(
                conn, "shipped", actor.user_id,
                {"shipped_quantity": shipped_total, "lines": shipped_lines,
                 "note": "医院实发后扣减库存"}, request_id,
            )
            self._promote_pending(conn, actor)
        return self.request_detail(actor, request_id)

    @staticmethod
    def _ship_overrides(payload: Dict[str, Any]) -> Dict[int, int]:
        raw = payload.get("items")
        if raw is None:
            return {}
        if not isinstance(raw, list):
            raise ValidationError("items必须是 [{allocation_id, quantity}] 列表")
        overrides: Dict[int, int] = {}
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("allocation_id"), int) or not isinstance(item.get("quantity"), int):
                raise ValidationError("items项必须包含整数allocation_id与quantity")
            overrides[int(item["allocation_id"])] = int(item["quantity"])
        return overrides

    # ---- 取消 ----
    def cancel(self, actor: Actor, request_id: int, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, {"dispatcher", "blood_bank", "hospital"})
        reason = (payload or {}).get("reason", "主动取消")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("取消原因不能为空")
        with self.repository.transaction() as conn:
            request = self.repository.get_request(conn, request_id)
            self.rules.require_transition(request["state"], "cancel")
            if request["state"] == ST_HELD:
                self.repository.release_holds(conn, request_id, reason)
            self.repository.update_request_state(conn, request_id, "cancelled", None)
            self.repository.add_event(conn, "cancelled", actor.user_id, {"reason": reason}, request_id)
            if request["state"] == ST_HELD:
                self._promote_pending(conn, actor)
        return self.request_detail(actor, request_id)

    # ---- 超时/过期清扫 ----
    def sweep(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._check_role(actor, {"blood_bank", "dispatcher", "admin"})
        with self.repository.transaction() as conn:
            released = self._sweep_locks(conn, actor)
            self._promote_pending(conn, actor)
        return {"released": released}

    def _sweep_locks(self, conn, actor: Actor) -> List[Dict[str, Any]]:
        """释放超时占用，并把锁库期间过期的批次占用退回排队。"""
        now = self._now()
        today = now.date()
        released: List[Dict[str, Any]] = []
        for request in self.repository.held_requests(conn):
            holds = self.repository.allocations_for(conn, request["id"], "held")
            reason = None
            if self.rules.hold_expired(request["held_expires_at"], now):
                reason = "锁库超时自动释放"
            elif any(not self.rules.is_fresh(h["expires_on"], today) for h in holds):
                reason = "批次过期自动释放"
            if reason:
                self._release_and_requeue(conn, request, reason, actor)
                released.append({"request_id": request["id"], "reason": reason})
        return released

    def _release_and_requeue(self, conn, request: Dict[str, Any], reason: str, actor: Actor) -> None:
        self.repository.release_holds(conn, request["id"], reason)
        self.repository.update_request_state(conn, request["id"], ST_PENDING, None)
        self.repository.add_event(
            conn, "released", actor.user_id,
            {"reason": reason, "note": "原占用已释放，请求回到队首等待重新确认"}, request["id"],
        )

    # ---- 查询 ----
    def list_hospitals(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        with self.repository._connect() as conn:
            return self.repository.list_hospitals(conn)

    def list_batches(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        with self.repository._connect() as conn:
            return self.repository.inventory_view(conn, self._today(self._now()))

    def _batch_view(self, batch_id: int) -> Dict[str, Any]:
        with self.repository._connect() as conn:
            row = self.repository.get_batch(conn, batch_id)
            held = self.repository.held_quantities_by_batch(conn).get(batch_id, 0)
        item = dict(row)
        item["held_quantity"] = held
        item["available_quantity"] = int(item["quantity"]) - held
        return item

    def list_casualties(self, actor: Actor) -> List[Dict[str, Any]]:
        self._actor(actor)
        with self.repository._connect() as conn:
            return self.repository.list_casualties(conn)

    def list_requests(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        self._actor(actor)
        with self.repository._connect() as conn:
            rows = self.repository.list_requests(conn, state)
            for row in rows:
                row["allocations"] = self.repository.allocations_for(conn, row["id"])
            return rows

    def request_detail(self, actor: Actor, request_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        with self.repository._connect() as conn:
            request = self.repository.get_request(conn, request_id)
            request["allocations"] = self.repository.allocations_for(conn, request_id)
            request["events"] = self.repository.events_for(conn, request_id)
            return request

    def trace_casualty(self, actor: Actor, casualty_id: int) -> Dict[str, Any]:
        """沿伤员查到调剂去向：每个请求的锁库/实发明细与台账。"""
        actor = self._actor(actor)
        with self.repository._connect() as conn:
            return self.repository.trace_casualty(conn, casualty_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        self._actor(actor)
        with self.repository._connect() as conn:
            today = self._today(self._now())
            inv = self.repository.inventory_view(conn, today)
            requests = self.repository.list_requests(conn)
        states: Dict[str, int] = {}
        for request in requests:
            states[request["state"]] = states.get(request["state"], 0) + 1
        return {
            "requests": states,
            "batches": len(inv),
            "expired_batches": sum(1 for item in inv if item["expired"]),
            "units_in_stock": sum(int(item["quantity"]) for item in inv),
            "units_held": sum(item["held_qty"] for item in inv),
            "units_shippable": sum(0 if item["expired"] else item["available_qty"] for item in inv),
        }

    def health(self) -> bool:
        return self.repository.health()
