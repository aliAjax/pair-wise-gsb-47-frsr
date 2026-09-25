"""群体伤亡医院应急扩容协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "reported"
CREATE_ROLES = {'incident_commander', 'transport_coordinator'}
ACTION_ROLES = {'allocate': {'incident_commander', 'transport_coordinator'}, 'accept': {'hospital_liaison'}, 'transfer': {'hospital_liaison', 'transport_coordinator'}, 'complete': {'hospital_liaison'}, 'cancel': {'incident_commander'}}
TRANSITIONS = {'allocate': {'reported': 'allocated'}, 'accept': {'allocated': 'accepted'}, 'transfer': {'accepted': 'transferred'}, 'complete': {'transferred': 'closed'}, 'cancel': {'reported': 'cancelled', 'allocated': 'cancelled', 'accepted': 'cancelled'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "hospital")
        integer(p, "casualty_count", 1)
        choice(p, "triage", ["red", "yellow", "green", "black"])
        integer(p, "required_beds", 0)
        integer(p, "required_ventilators", 0)
        integer(p, "hospital_beds", 0)
        integer(p, "hospital_ventilators", 0)
        integer(p, "transport_units", 0)
        integer(p, "transport_minutes", 0)
        ratio = number(p, "reserve_ratio", 0, 1)
        if p["required_beds"] > p["hospital_beds"]:
            raise ValidationError("所需床位超过医院容量")
        if p["required_ventilators"] > p["hospital_ventilators"]:
            raise ValidationError("所需呼吸机超过医院容量")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        demand_weights = {"red": 3, "yellow": 2, "green": 1, "black": 0}
        p["demand_points"] = demand_weights[p["triage"]] * int(p["casualty_count"])
        p["required_beds"] = max(int(p["required_beds"]), demand_weights[p["triage"]] * int(p["casualty_count"]))
        p["required_ambulances"] = (int(p["casualty_count"]) + 1) // 2
        p["load_ratio"] = round(p["required_beds"] / max(1, (1 - float(p["reserve_ratio"])) * int(p["hospital_beds"])), 3)
        p["capacity_ok"] = bool(p["load_ratio"] <= 1.0)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        hospital = payload.get("hospital")
        for item in existing:
            if item["state"] in {"closed", "cancelled"} or item["payload"].get("hospital") != hospital:
                continue
            used = int(item["payload"].get("required_beds", 0))
            reserve = float(item["payload"].get("reserve_ratio", 0))
            capacity = int(item["payload"].get("hospital_beds", 0)) * (1 - reserve)
            if used + int(payload.get("required_beds", 0)) > capacity:
                raise Conflict("该医院剩余应急容量不足")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "allocate":
            if float(p["transport_units"]) < float(p["required_ambulances"]):
                raise ValidationError("转运单元不足")
            if not p["capacity_ok"]:
                raise ValidationError("医院剩余容量不足")
            changes["allocation_confirmed"] = True
            summary = "已生成医院接收分配"
        elif action == "accept":
            if not boolean(data, "liaison_acceptance"):
                raise ValidationError("医院未确认接收")
            if integer(data, "updated_available_beds", 0) < int(p["required_beds"]):
                raise ValidationError("更新后的可用床位不足")
            changes["liaison_acceptance"] = True
            summary = "医院已接受分配"
        elif action == "transfer":
            if not boolean(data, "vehicle_assigned"):
                raise ValidationError("尚未安排转运车辆")
            changes["vehicle_assigned"] = True
            summary = "伤员开始转运"
        elif action == "complete":
            changes["outcome"] = choice(data, "outcome", ["treated", "transferred", "expired"])
            if not boolean(data, "documentation_complete"):
                raise ValidationError("交接文书未完成")
            summary = "接收流程结束"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "分配取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
