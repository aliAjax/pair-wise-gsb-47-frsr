"""血液调剂规则：血型相容、候选排序、状态转换与输入校验。

纯函数为主，不接触数据库，便于单独测试。
"""
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Conflict, ValidationError, choice, integer, number, text


# 红细胞：供者 -> 可接受的受者（O- 万能供血，AB+ 万能受血）
RBC_DONOR_TO_RECIPIENTS = {
    "O-":  {"O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"},
    "O+":  {"O+", "A+", "B+", "AB+"},
    "A-":  {"A-", "A+", "AB-", "AB+"},
    "A+":  {"A+", "AB+"},
    "B-":  {"B-", "B+", "AB-", "AB+"},
    "B+":  {"B+", "AB+"},
    "AB-": {"AB-", "AB+"},
    "AB+": {"AB+"},
}

# 血浆：抗体规则与红细胞相反，AB 为万能供血
PLASMA_DONOR_TO_RECIPIENTS = {
    "AB":  {"O", "A", "B", "AB"},
    "A":   {"A", "O"},
    "B":   {"B", "O"},
    "O":   {"O"},
}

BLOOD_TYPES = ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]
COMPONENTS = ["红细胞", "血浆", "血小板"]

# 请求/占用状态
ST_PENDING = "pending"        # 库存不足，排队待确认（未占用任何血袋）
ST_HELD = "held"              # 已全部锁库，等待医院实发
ST_SHIPPED = "shipped"        # 医院已实发，库存已扣减
ST_CANCELLED = "cancelled"    # 取消
ST_TIMEDOUT = "timed_out"     # 锁库超时被释放

ALLOC_HELD = "held"
ALLOC_SHIPPED = "shipped"
ALLOC_RELEASED = "released"

# 可执行的动作与目标状态
TRANSITIONS = {
    "ship":    {ST_HELD: ST_SHIPPED},
    "cancel":  {ST_PENDING: ST_CANCELLED, ST_HELD: ST_CANCELLED},
    "timeout": {ST_HELD: ST_TIMEDOUT},
}


class BloodRules:
    """血型相容、效期/距离排序与请求状态机。"""

    # ---- 血型相容 ----
    def compatible(self, component: str, donor_type: str, patient_type: str) -> bool:
        if component == "红细胞":
            return patient_type in RBC_DONOR_TO_RECIPIENTS.get(donor_type, set())
        if component == "血浆":
            return self._plasma_base(patient_type) in PLASMA_DONOR_TO_RECIPIENTS.get(
                self._plasma_base(donor_type), set()
            )
        # 血小板：按红细胞的 Rh/ABO 相容表处理（临床实践近似）
        return patient_type in RBC_DONOR_TO_RECIPIENTS.get(donor_type, set())

    @staticmethod
    def _plasma_base(blood_type: str) -> str:
        return blood_type[:-1] if blood_type.endswith(("-", "+")) else blood_type

    # ---- 效期 ----
    @staticmethod
    def parse_date(value: Any, field: str = "expires_on") -> date:
        if not isinstance(value, str):
            raise ValidationError("%s必须是YYYY-MM-DD日期" % field)
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValidationError("%s必须是YYYY-MM-DD日期" % field) from exc

    def is_fresh(self, expires_on: str, today: date) -> bool:
        """过期当天仍可用，次日起排除。"""
        return self.parse_date(expires_on) >= today

    # ---- 校验 ----
    def validate_hospital(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "name")
        number(p, "distance_km", 0)
        p["distance_km"] = round(float(p["distance_km"]), 1)
        return p

    def validate_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        choice(p, "blood_type", BLOOD_TYPES)
        choice(p, "component", COMPONENTS)
        integer(p, "quantity", 1)
        self.parse_date(p.get("expires_on"), "expires_on")
        return p

    def validate_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        integer(p, "casualty_id", 1)
        choice(p, "blood_type", BLOOD_TYPES)
        choice(p, "component", COMPONENTS)
        integer(p, "quantity", 1)
        return p

    # ---- 候选与分配 ----
    def candidate_batches(
        self,
        request: Dict[str, Any],
        batches: Iterable[Dict[str, Any]],
        held_by_batch: Dict[int, int],
        today: date,
    ) -> List[Dict[str, Any]]:
        """返回候选血袋：同成分、血型相容、未过期、仍有未占用余量。

        排序：效期近的先用（FEFO），效期相同则距离近的先用，再以批次号兜底。
        """
        candidates: List[Dict[str, Any]] = []
        for batch in batches:
            if batch["component"] != request["component"]:
                continue
            if not self.compatible(request["component"], batch["blood_type"], request["blood_type"]):
                continue
            if not self.is_fresh(batch["expires_on"], today):
                continue
            free = int(batch["quantity"]) - held_by_batch.get(int(batch["id"]), 0)
            if free <= 0:
                continue
            enriched = dict(batch)
            enriched["free_quantity"] = free
            candidates.append(enriched)
        candidates.sort(
            key=lambda b: (self.parse_date(b["expires_on"]), float(b["distance_km"]), int(b["id"]))
        )
        return candidates

    def plan_allocation(
        self,
        request: Dict[str, Any],
        batches: Iterable[Dict[str, Any]],
        held_by_batch: Dict[int, int],
        today: date,
    ) -> Tuple[List[Dict[str, int]], int]:
        """贪心选袋，返回 [(batch_id, qty), ...] 与可满足数量。"""
        remaining = int(request["quantity"])
        plan: List[Dict[str, int]] = []
        for batch in self.candidate_batches(request, batches, held_by_batch, today):
            if remaining <= 0:
                break
            take = min(remaining, int(batch["free_quantity"]))
            plan.append({"batch_id": int(batch["id"]), "quantity": take})
            remaining -= take
        satisfied = int(request["quantity"]) - remaining
        return plan, satisfied

    # ---- 状态机 ----
    def require_transition(self, state: str, action: str) -> str:
        target = TRANSITIONS.get(action, {}).get(state)
        if target is None:
            raise Conflict("当前状态%s不允许执行%s" % (state, action))
        return target

    def hold_expired(self, expires_at: Optional[str], now: datetime) -> bool:
        if not expires_at:
            return False
        return datetime.fromisoformat(expires_at) <= now
