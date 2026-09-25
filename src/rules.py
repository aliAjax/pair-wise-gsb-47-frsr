"""血液调剂台领域规则：血型相容、候选排序、占用计划与状态转换。

本模块只做纯计算，不接触存储；时间全部由调用方传入，便于测试。
"""
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, ValidationError, choice, integer, number, optional_text, text


LOT_KIND = "blood_lot"
REQUEST_KIND = "blood_request"
ALLOCATION_KIND = "allocation"

BLOOD_TYPES = ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]
COMPONENTS = ["whole_blood", "rbc", "plasma", "platelets", "cryo"]
PRIORITIES = ["critical", "urgent", "routine"]

LOT_STATES = ["registered", "expired", "closed"]
REQUEST_STATES = ["requested", "matched", "fulfilled", "cancelled"]
ALLOCATION_STATES = ["reserved", "pending", "issued", "released"]

ALLOCATION_TRANSITIONS = {
    "issue": {"reserved": "issued"},
    "confirm": {"pending": "reserved"},
    "cancel": {"reserved": "released", "pending": "released"},
    "expire": {"reserved": "released", "pending": "released"},
}
REQUEST_TRANSITIONS = {"cancel": {"requested": "cancelled", "matched": "cancelled"}}
LOT_TRANSITIONS = {"close": {"registered": "closed"}}

REGISTER_LOT_ROLES = {"blood_bank_clerk", "hospital_liaison"}
CREATE_REQUEST_ROLES = {"incident_commander", "hospital_liaison"}
MATCH_ROLES = {"blood_bank_clerk", "incident_commander"}
ALLOCATION_ACTION_ROLES = {
    "issue": {"hospital_liaison"},
    "confirm": {"blood_bank_clerk", "hospital_liaison"},
    "cancel": {"blood_bank_clerk", "hospital_liaison", "incident_commander"},
    "expire": {"blood_bank_clerk"},
}
REQUEST_ACTION_ROLES = {
    "cancel": {"incident_commander", "hospital_liaison"},
    "match": MATCH_ROLES,
}
LOT_ACTION_ROLES = {"close": {"hospital_liaison", "blood_bank_clerk"}}

DEFAULT_HOLD_MINUTES = 30

# 红细胞/全血：献血者 -> 可接受的受血者（O- 万能供者，AB+ 万能受者）
_RBC_DONOR = {
    "O-": {"O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"},
    "O+": {"O+", "A+", "B+", "AB+"},
    "A-": {"A-", "A+", "AB-", "AB+"},
    "A+": {"A+", "AB+"},
    "B-": {"B-", "B+", "AB-", "AB+"},
    "B+": {"B+", "AB+"},
    "AB-": {"AB-", "AB+"},
    "AB+": {"AB+"},
}
# 血浆：捐献方向与红细胞相反（AB 万能血浆供者，O 只能接受 O）
_PLASMA_DONOR = {
    "O-": {"O-", "O+"},
    "O+": {"O+"},
    "A-": {"A-", "A+", "O-", "O+"},
    "A+": {"A+", "O+"},
    "B-": {"B-", "B+", "O-", "O+"},
    "B+": {"B+", "O+"},
    "AB-": {"O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"},
    "AB+": {"O+", "A+", "B+", "AB+"},
}


def parse_moment(value: Any, key: str = "expires_at") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % key)
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s必须是ISO时间" % key) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def compatible(component: str, donor_type: str, recipient_type: str) -> bool:
    """判断献血者血型能否输给受血者。血小板/冷沉淀简化要求同型。"""
    if component in ("whole_blood", "rbc"):
        return recipient_type in _RBC_DONOR[donor_type]
    if component == "plasma":
        return recipient_type in _PLASMA_DONOR[donor_type]
    return donor_type == recipient_type


def distance_km(a: Optional[Dict[str, float]], b: Optional[Dict[str, float]]) -> Optional[float]:
    if not a or not b:
        return None
    lat1, lon1, lat2, lon2 = (radians(v) for v in (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return round(6371.0 * 2 * asin(sqrt(h)), 2)


class DomainRules:
    def known_role(self, role: str) -> bool:
        all_roles = set(REGISTER_LOT_ROLES) | set(CREATE_REQUEST_ROLES) | set(MATCH_ROLES)
        for roles in ALLOCATION_ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in REQUEST_ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in LOT_ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def can_register_lot(self, role: str) -> bool:
        return role == "admin" or role in REGISTER_LOT_ROLES

    def can_create_request(self, role: str) -> bool:
        return role == "admin" or role in CREATE_REQUEST_ROLES

    def can_allocation_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ALLOCATION_ACTION_ROLES.get(action, set())

    def can_request_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in REQUEST_ACTION_ROLES.get(action, set())

    def can_lot_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in LOT_ACTION_ROLES.get(action, set())

    @staticmethod
    def _location(payload: Dict[str, Any]) -> Optional[Dict[str, float]]:
        if payload.get("lat") is None and payload.get("lng") is None:
            return None
        return {"lat": number(payload, "lat", -90, 90), "lng": number(payload, "lng", -180, 180)}

    def prepare_lot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """医院登记血袋批次：血型、成分、效期、可调剂数量。"""
        p = dict(payload or {})
        quantity = integer(p, "quantity", 1)
        return {
            "hospital": text(p, "hospital"),
            "blood_type": choice(p, "blood_type", BLOOD_TYPES),
            "component": choice(p, "component", COMPONENTS),
            "expires_at": iso(parse_moment(p.get("expires_at"))),
            "quantity_total": quantity,
            "quantity_available": quantity,
            "quantity_reserved": 0,
            "quantity_issued": 0,
            "location": self._location(p),
            "note": optional_text(p, "note"),
        }

    def prepare_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """伤员登记所需血型与用量。"""
        p = dict(payload or {})
        priority = optional_text(p, "priority", "urgent") or "urgent"
        if priority not in PRIORITIES:
            raise ValidationError("priority只能是%s" % "/".join(PRIORITIES))
        return {
            "casualty_ref": text(p, "casualty_ref"),
            "blood_type": choice(p, "blood_type", BLOOD_TYPES),
            "component": choice(p, "component", COMPONENTS),
            "units_needed": integer(p, "units", 1),
            "units_reserved": 0,
            "units_issued": 0,
            "units_pending": 0,
            "coverage": "none",
            "priority": priority,
            "location": self._location(p),
            "note": optional_text(p, "note"),
        }

    def is_expired(self, lot_payload: Dict[str, Any], now: datetime) -> bool:
        return parse_moment(lot_payload["expires_at"]) <= now

    def is_candidate(self, lot: Dict[str, Any], request_payload: Dict[str, Any], now: datetime) -> bool:
        """候选批次：同成分、血型相容、未过期。过期血袋不能进候选。"""
        payload = lot["payload"]
        if lot["state"] != "registered":
            return False
        if payload["component"] != request_payload["component"]:
            return False
        if not compatible(payload["component"], payload["blood_type"], request_payload["blood_type"]):
            return False
        return not self.is_expired(payload, now)

    def rank_candidates(self, lots: List[Dict[str, Any]], request_payload: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
        """匹配排序：先看相容血型与效期，再按距离由近到远。"""
        candidates = [lot for lot in lots if self.is_candidate(lot, request_payload, now)]

        def key(lot: Dict[str, Any]) -> Tuple[datetime, float]:
            distance = distance_km(lot["payload"].get("location"), request_payload.get("location"))
            return (parse_moment(lot["payload"]["expires_at"]), distance if distance is not None else float("inf"))

        return sorted(candidates, key=key)

    def plan_allocations(self, request_payload: Dict[str, Any], lots: List[Dict[str, Any]], now: datetime, units_open: int) -> List[Dict[str, Any]]:
        """贪心占用计划：可用量足够则reserved；不足部分挂在最优候选上pending（留待确认）。"""
        plan: List[Dict[str, Any]] = []
        if units_open <= 0:
            return plan
        candidates = self.rank_candidates(lots, request_payload, now)
        remaining = units_open
        for lot in candidates:
            available = int(lot["payload"].get("quantity_available", 0))
            if available <= 0:
                continue
            take = min(available, remaining)
            plan.append({"lot": lot, "units": take, "status": "reserved"})
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0 and candidates:
            plan.append({"lot": candidates[0], "units": remaining, "status": "pending"})
        return plan

    def hold_until(self, now: datetime, hold_minutes: int) -> str:
        return iso(now + timedelta(minutes=hold_minutes))

    @staticmethod
    def _require(transitions: Dict[str, Dict[str, str]], state: str, action: str) -> str:
        allowed = transitions.get(action, {}).get(state)
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def require_allocation_transition(self, allocation: Dict[str, Any], action: str) -> str:
        return self._require(ALLOCATION_TRANSITIONS, allocation["state"], action)

    def require_request_transition(self, request: Dict[str, Any], action: str) -> str:
        return self._require(REQUEST_TRANSITIONS, request["state"], action)

    def require_lot_transition(self, lot: Dict[str, Any], action: str) -> str:
        return self._require(LOT_TRANSITIONS, lot["state"], action)

    def request_rollups(self, payload: Dict[str, Any], allocations: List[Dict[str, Any]], current_state: str) -> Tuple[str, Dict[str, Any]]:
        """按调剂单实况汇总需求：占用/实发/留待确认数量与覆盖度。"""
        reserved = sum(a["payload"]["units"] for a in allocations if a["state"] == "reserved")
        issued = sum(a["payload"]["units"] for a in allocations if a["state"] == "issued")
        pending = sum(a["payload"]["units"] for a in allocations if a["state"] == "pending")
        covered = reserved + issued
        needed = int(payload["units_needed"])
        updates = {
            "units_reserved": reserved,
            "units_issued": issued,
            "units_pending": pending,
            "coverage": "full" if covered >= needed else ("partial" if covered > 0 else "none"),
        }
        if current_state == "cancelled":
            state = "cancelled"
        elif issued >= needed:
            state = "fulfilled"
        elif covered > 0:
            state = "matched"
        else:
            state = "requested"
        return state, updates
