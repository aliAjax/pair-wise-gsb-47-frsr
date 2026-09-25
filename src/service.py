"""血液调剂台用例编排：登记、匹配、占用、实发、释放与追溯。"""
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, optional_text, text
from .repository import Repository, Tx
from .rules import (
    ALLOCATION_KIND,
    DEFAULT_HOLD_MINUTES,
    LOT_KIND,
    REQUEST_KIND,
    DomainRules,
    distance_km,
    iso,
    parse_moment,
)


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None,
                 hold_minutes: int = DEFAULT_HOLD_MINUTES, clock: Callable[[], datetime] = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.hold_minutes = int(hold_minutes)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _ensure_kind(record: Dict[str, Any], kind: str) -> None:
        if record["kind"] != kind:
            raise NotFound("记录不存在")

    # ---------------------------------------------------------------- 登记

    def register_lot(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_register_lot(actor.role):
            raise PermissionDenied("角色无权登记血袋")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_lot(payload or {})
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
            return tx.create(LOT_KIND, reference, "registered", prepared, actor.user_id)

    def create_request(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """登记伤员用血需求并立即匹配：够用则占用，不够用则留待确认。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.can_create_request(actor.role):
            raise PermissionDenied("角色无权登记用血需求")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_request(payload or {})
        now = self.clock()
        with self.repository.transaction() as tx:
            self._sweep(tx, now)
            request = tx.create(REQUEST_KIND, reference, "requested", prepared, actor.user_id)
            self._match_locked(tx, request, actor.user_id, now)
            return self._request_detail_locked(tx, request["id"])

    # ---------------------------------------------------------------- 匹配

    def _allocations_of(self, tx: Tx, request_id: int) -> List[Dict[str, Any]]:
        return [a for a in tx.list_records(kind=ALLOCATION_KIND, limit=500) if a["payload"].get("request_id") == request_id]

    def _match_locked(self, tx: Tx, request: Dict[str, Any], actor_id: str, now: datetime) -> Dict[str, Any]:
        payload = request["payload"]
        units_open = int(payload["units_needed"]) - int(payload["units_reserved"]) - int(payload["units_issued"]) - int(payload["units_pending"])
        if units_open <= 0 or request["state"] in ("fulfilled", "cancelled"):
            return request
        lots = tx.list_records(kind=LOT_KIND, state="registered", limit=500)
        plan = self.rules.plan_allocations(payload, lots, now, units_open)
        seq = len(self._allocations_of(tx, request["id"])) + 1
        for item in plan:
            lot = item["lot"]
            if item["status"] == "reserved":
                lot_payload = dict(lot["payload"])
                lot_payload["quantity_available"] -= item["units"]
                lot_payload["quantity_reserved"] += item["units"]
                tx.mutate(lot["id"], lot["version"], lot["state"], lot_payload, actor_id, "reserve",
                          {"summary": "为%s预留%s单位" % (request["reference"], item["units"]), "request_id": request["id"]})
            allocation_payload = {
                "request_id": request["id"],
                "request_reference": request["reference"],
                "casualty_ref": payload["casualty_ref"],
                "lot_id": lot["id"],
                "lot_reference": lot["reference"],
                "hospital": lot["payload"]["hospital"],
                "blood_type": lot["payload"]["blood_type"],
                "component": lot["payload"]["component"],
                "units": item["units"],
                "distance_km": distance_km(lot["payload"].get("location"), payload.get("location")),
                "hold_until": self.rules.hold_until(now, self.hold_minutes),
                "released_at": "",
                "release_reason": "",
                "issued_at": "",
                "issued_by": "",
            }
            tx.create(ALLOCATION_KIND, "%s-A%d" % (request["reference"], seq), item["status"], allocation_payload, actor_id)
            seq += 1
        return self._refresh_request_locked(tx, request["id"], actor_id)

    def _refresh_request_locked(self, tx: Tx, request_id: int, actor_id: str) -> Dict[str, Any]:
        request = tx.get(request_id)
        state, updates = self.rules.request_rollups(request["payload"], self._allocations_of(tx, request_id), request["state"])
        new_payload = dict(request["payload"])
        new_payload.update(updates)
        if state != request["state"] or new_payload != request["payload"]:
            return tx.mutate(request_id, request["version"], state, new_payload, actor_id, "refresh",
                             {"summary": "需求汇总更新", "to": state})
        return request

    # ---------------------------------------------------------------- 调剂单动作

    def allocation_action(self, actor: Actor, allocation_id: int, action: str, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.can_allocation_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        now = self.clock()
        with self.repository.transaction() as tx:
            self._sweep(tx, now)
            allocation = tx.get(allocation_id)
            self._ensure_kind(allocation, ALLOCATION_KIND)
            self.rules.require_allocation_transition(allocation, action)
            if action == "issue":
                return self._issue_locked(tx, allocation, actor, int(expected_version), now)
            if action == "confirm":
                return self._confirm_locked(tx, allocation, actor, int(expected_version), now)
            reason = optional_text(data or {}, "reason") or ("timeout" if action == "expire" else "cancelled")
            return self._release_locked(tx, allocation, reason, actor.user_id, now, expected_version=int(expected_version))

    def _issue_locked(self, tx: Tx, allocation: Dict[str, Any], actor: Actor, expected_version: int, now: datetime) -> Dict[str, Any]:
        """医院实发：直到此刻才真正扣库存；过期血袋禁止实发。"""
        lot = tx.get(allocation["payload"]["lot_id"])
        if lot["state"] != "registered":
            raise Conflict("血袋批次当前不可发血")
        if self.rules.is_expired(lot["payload"], now):
            raise Conflict("血袋已过期，禁止实发")
        units = int(allocation["payload"]["units"])
        lot_payload = dict(lot["payload"])
        lot_payload["quantity_reserved"] -= units
        lot_payload["quantity_issued"] += units
        tx.mutate(lot["id"], lot["version"], lot["state"], lot_payload, actor.user_id, "issue",
                  {"summary": "实发%s单位，库存扣减" % units, "allocation_id": allocation["id"]})
        payload = dict(allocation["payload"])
        payload["issued_at"] = iso(now)
        payload["issued_by"] = actor.user_id
        updated = tx.mutate(allocation["id"], expected_version, "issued", payload, actor.user_id, "issue",
                            {"summary": "医院实发%s单位" % units})
        self._refresh_request_locked(tx, allocation["payload"]["request_id"], actor.user_id)
        return updated

    def _confirm_locked(self, tx: Tx, allocation: Dict[str, Any], actor: Actor, expected_version: int, now: datetime) -> Dict[str, Any]:
        """留待确认转为正式占用：库存仍不足则继续等待。"""
        lot = tx.get(allocation["payload"]["lot_id"])
        if lot["state"] != "registered":
            raise Conflict("血袋批次当前不可确认")
        if self.rules.is_expired(lot["payload"], now):
            raise Conflict("血袋已过期，无法确认")
        units = int(allocation["payload"]["units"])
        if int(lot["payload"]["quantity_available"]) < units:
            raise Conflict("可用库存仍不足，继续留待确认")
        lot_payload = dict(lot["payload"])
        lot_payload["quantity_available"] -= units
        lot_payload["quantity_reserved"] += units
        tx.mutate(lot["id"], lot["version"], lot["state"], lot_payload, actor.user_id, "confirm",
                  {"summary": "确认占用%s单位" % units, "allocation_id": allocation["id"]})
        payload = dict(allocation["payload"])
        payload["hold_until"] = self.rules.hold_until(now, self.hold_minutes)
        updated = tx.mutate(allocation["id"], expected_version, "reserved", payload, actor.user_id, "confirm",
                            {"summary": "留待确认转为正式占用"})
        self._refresh_request_locked(tx, allocation["payload"]["request_id"], actor.user_id)
        return updated

    def _release_locked(self, tx: Tx, allocation: Dict[str, Any], reason: str, actor_id: str, now: datetime,
                        expected_version: int = None) -> Dict[str, Any]:
        """取消或超时释放：预留量回补可调剂库存。"""
        units = int(allocation["payload"]["units"])
        if allocation["state"] == "reserved":
            lot = tx.get(allocation["payload"]["lot_id"])
            lot_payload = dict(lot["payload"])
            lot_payload["quantity_available"] += units
            lot_payload["quantity_reserved"] -= units
            tx.mutate(lot["id"], lot["version"], lot["state"], lot_payload, actor_id, "release",
                      {"summary": "释放预留%s单位(%s)" % (units, reason), "allocation_id": allocation["id"]})
        payload = dict(allocation["payload"])
        payload["released_at"] = iso(now)
        payload["release_reason"] = reason
        version = allocation["version"] if expected_version is None else expected_version
        updated = tx.mutate(allocation["id"], version, "released", payload, actor_id, "release",
                            {"summary": "占用释放(%s)" % reason})
        self._refresh_request_locked(tx, allocation["payload"]["request_id"], actor_id)
        return updated

    def _sweep(self, tx: Tx, now: datetime) -> None:
        """超时占用自动释放；过期血袋移出候选并释放其占用。"""
        for state in ("reserved", "pending"):
            for allocation in tx.list_records(kind=ALLOCATION_KIND, state=state, limit=500):
                hold_until = allocation["payload"].get("hold_until")
                if hold_until and parse_moment(hold_until, "hold_until") <= now:
                    self._release_locked(tx, allocation, "timeout", "system", now)
        for lot in tx.list_records(kind=LOT_KIND, state="registered", limit=500):
            if not self.rules.is_expired(lot["payload"], now):
                continue
            for state in ("reserved", "pending"):
                for allocation in tx.list_records(kind=ALLOCATION_KIND, state=state, limit=500):
                    if allocation["payload"].get("lot_id") == lot["id"]:
                        self._release_locked(tx, allocation, "lot_expired", "system", now)
            fresh = tx.get(lot["id"])
            tx.mutate(lot["id"], fresh["version"], "expired", fresh["payload"], "system", "expire_lot",
                      {"summary": "血袋过期，退出候选"})

    # ---------------------------------------------------------------- 需求与批次动作

    def request_action(self, actor: Actor, request_id: int, action: str, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.can_request_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        now = self.clock()
        with self.repository.transaction() as tx:
            self._sweep(tx, now)
            request = tx.get(request_id)
            self._ensure_kind(request, REQUEST_KIND)
            if int(expected_version) != int(request["version"]):
                raise Conflict("版本冲突，请刷新后重试")
            if action == "match":
                if request["state"] in ("fulfilled", "cancelled"):
                    raise Conflict("当前状态不允许执行match")
                self._match_locked(tx, request, actor.user_id, now)
            else:
                self.rules.require_request_transition(request, action)
                for allocation in self._allocations_of(tx, request_id):
                    if allocation["state"] in ("reserved", "pending"):
                        self._release_locked(tx, allocation, "request_cancelled", actor.user_id, now)
                fresh = tx.get(request_id)
                payload = dict(fresh["payload"])
                payload["cancel_reason"] = optional_text(data or {}, "reason")
                tx.mutate(request_id, fresh["version"], "cancelled", payload, actor.user_id, "cancel",
                          {"summary": "用血需求取消"})
            return self._request_detail_locked(tx, request_id)

    def lot_action(self, actor: Actor, lot_id: int, action: str, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.can_lot_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        now = self.clock()
        with self.repository.transaction() as tx:
            self._sweep(tx, now)
            lot = tx.get(lot_id)
            self._ensure_kind(lot, LOT_KIND)
            self.rules.require_lot_transition(lot, action)
            active = [a for a in tx.list_records(kind=ALLOCATION_KIND, limit=500)
                      if a["payload"].get("lot_id") == lot_id and a["state"] in ("reserved", "pending")]
            if active:
                raise Conflict("仍有未完成的调剂占用，不能关闭")
            return tx.mutate(lot_id, int(expected_version), "closed", lot["payload"], actor.user_id, "close",
                             {"summary": "批次关闭"})

    # ---------------------------------------------------------------- 查询与追溯

    def _request_detail_locked(self, tx: Tx, request_id: int) -> Dict[str, Any]:
        request = tx.get(request_id)
        self._ensure_kind(request, REQUEST_KIND)
        return {"request": request, "allocations": self._allocations_of(tx, request_id)}

    def request_detail(self, actor: Actor, request_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
            return self._request_detail_locked(tx, request_id)

    def lot_detail(self, actor: Actor, lot_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
            lot = tx.get(lot_id)
            self._ensure_kind(lot, LOT_KIND)
            allocations = [a for a in tx.list_records(kind=ALLOCATION_KIND, limit=500) if a["payload"].get("lot_id") == lot_id]
            return {"lot": lot, "allocations": allocations}

    def list_lots(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
            return tx.list_records(kind=LOT_KIND, state=state, limit=limit)

    def list_requests(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
            return tx.list_records(kind=REQUEST_KIND, state=state, limit=limit)

    def list_allocations(self, actor: Actor, state: Optional[str] = None, casualty_ref: Optional[str] = None,
                         request_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
            allocations = tx.list_records(kind=ALLOCATION_KIND, state=state, limit=limit)
        if casualty_ref:
            allocations = [a for a in allocations if a["payload"].get("casualty_ref") == casualty_ref]
        if request_id is not None:
            allocations = [a for a in allocations if a["payload"].get("request_id") == int(request_id)]
        return allocations

    def casualty_trail(self, actor: Actor, casualty_ref: str) -> Dict[str, Any]:
        """沿伤员查调剂去向：需求、每张调剂单、供血批次与各自时间线。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        casualty_ref = text({"casualty_ref": casualty_ref}, "casualty_ref")
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
            requests = [r for r in tx.list_records(kind=REQUEST_KIND, limit=500) if r["payload"].get("casualty_ref") == casualty_ref]
            if not requests:
                raise NotFound("未找到该伤员的用血登记")
            entries = []
            for request in requests:
                allocations = []
                for allocation in self._allocations_of(tx, request["id"]):
                    lot = tx.get(allocation["payload"]["lot_id"])
                    allocations.append({
                        "allocation": allocation,
                        "lot": {
                            "id": lot["id"],
                            "reference": lot["reference"],
                            "state": lot["state"],
                            "hospital": lot["payload"]["hospital"],
                            "blood_type": lot["payload"]["blood_type"],
                            "component": lot["payload"]["component"],
                            "expires_at": lot["payload"]["expires_at"],
                        },
                        "timeline": tx.audit_timeline(allocation["id"]),
                    })
                entries.append({"request": request, "timeline": tx.audit_timeline(request["id"]), "allocations": allocations})
            return {"casualty_ref": casualty_ref, "requests": entries}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Dict[str, int]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository.transaction() as tx:
            self._sweep(tx, self.clock())
        return self.repository.stats()
