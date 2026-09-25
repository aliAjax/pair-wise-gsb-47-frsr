"""端到端调剂流程：登记→匹配→抢占→实发→取消/超时→重启溯源。"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


BANK = Actor("banker", "blood_bank")
DISPATCH = Actor("coord", "dispatcher")
HOSP = Actor("hospital", "hospital")


def future(days=14):
    return (date.today() + timedelta(days=days)).isoformat()


def past(days=1):
    return (date.today() - timedelta(days=days)).isoformat()


class BloodDeskTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "blood.db")
        self.service = build_service(self.db_path, hold_ttl_seconds=300)

    def tearDown(self):
        self.temp.cleanup()

    def _register(self, name="一院", distance=5.0, blood="O+", component="红细胞", qty=4, expires=None, batch_no=None):
        hospital = self.service.register_hospital(BANK, {"name": name, "distance_km": distance})
        payload = {"blood_type": blood, "component": component, "quantity": qty, "expires_on": expires or future()}
        if batch_no:
            payload["batch_no"] = batch_no
        batch = self.service.register_batch(BANK, hospital["id"], payload)
        return hospital, batch

    def _casualty(self, blood="O+", name="伤员甲"):
        return self.service.register_casualty(DISPATCH, {"name": name, "blood_type": blood})

    def test_full_flow_match_hold_ship_deduct(self):
        _, batch = self._register(qty=4)
        casualty = self._casualty()
        request = self.service.create_request(DISPATCH, {"casualty_id": casualty["id"], "blood_type": "O+",
                                                         "component": "红细胞", "quantity": 2})
        self.assertEqual(request["state"], "held")
        self.assertEqual(len(request["allocations"]), 1)
        self.assertEqual(request["allocations"][0]["quantity"], 2)
        # 库存尚未扣减：在库4，占用2，可调剂2
        inv = {b["batch_no"]: b for b in self.service.list_batches(BANK)}
        self.assertEqual(inv[batch["batch_no"]]["quantity"], 4)
        self.assertEqual(inv[batch["batch_no"]]["held_qty"], 2)
        # 医院实发后才扣库存
        shipped = self.service.ship(HOSP, request["id"], {})
        self.assertEqual(shipped["state"], "shipped")
        self.assertEqual(shipped["shipped_quantity"], 2)
        inv = {b["batch_no"]: b for b in self.service.list_batches(BANK)}
        self.assertEqual(inv[batch["batch_no"]]["quantity"], 2)
        self.assertEqual(inv[batch["batch_no"]]["held_qty"], 0)
        # 已实发不能再取消
        from src.domain import Conflict as C
        with self.assertRaises(C):
            self.service.cancel(DISPATCH, request["id"], {"reason": "x"})

    def test_compatible_match_prefers_fefo_and_distance(self):
        near = self.service.register_hospital(BANK, {"name": "近院", "distance_km": 3})
        far = self.service.register_hospital(BANK, {"name": "远院", "distance_km": 80})
        soon = self.service.register_batch(BANK, far["id"], {"blood_type": "O+", "component": "红细胞",
                                                             "quantity": 2, "expires_on": future(3)})
        later = self.service.register_batch(BANK, near["id"], {"blood_type": "O+", "component": "红细胞",
                                                               "quantity": 2, "expires_on": future(30)})
        casualty = self._casualty()
        request = self.service.create_request(DISPATCH, {"casualty_id": casualty["id"], "blood_type": "O+",
                                                         "component": "红细胞", "quantity": 2})
        # 效期优先于距离：先用远院近效期的批次
        self.assertEqual(request["allocations"][0]["batch_id"], soon["id"])

    def test_two_requesters_grab_same_batch_second_waits(self):
        _, batch = self._register(qty=3)
        c1 = self._casualty(name="伤员1")
        c2 = self._casualty(name="伤员2")
        r1 = self.service.create_request(DISPATCH, {"casualty_id": c1["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 3})
        r2 = self.service.create_request(DISPATCH, {"casualty_id": c2["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 2})
        self.assertEqual(r1["state"], "held")
        # 同批被抢空，后提交者留待确认，且不占用任何血袋
        self.assertEqual(r2["state"], "pending")
        self.assertEqual(r2["allocations"], [])
        inv = {b["batch_no"]: b for b in self.service.list_batches(BANK)}[batch["batch_no"]]
        self.assertEqual(inv["held_qty"], 3)
        # 第一家实发扣库后仍无货，第二家继续排队
        self.service.ship(HOSP, r1["id"], {})
        r2 = self.service.request_detail(DISPATCH, r2["id"])
        self.assertEqual(r2["state"], "pending")

    def test_cancel_held_releases_and_promotes_waiting(self):
        _, batch = self._register(qty=3)
        c1 = self._casualty(name="伤员1")
        c2 = self._casualty(name="伤员2")
        r1 = self.service.create_request(DISPATCH, {"casualty_id": c1["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 3})
        r2 = self.service.create_request(DISPATCH, {"casualty_id": c2["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 3})
        self.assertEqual(r2["state"], "pending")
        self.service.cancel(DISPATCH, r1["id"], {"reason": "伤员转院"})
        r2 = self.service.request_detail(DISPATCH, r2["id"])
        self.assertEqual(r2["state"], "held")
        self.assertEqual(r2["allocations"][0]["batch_id"], batch["id"])

    def test_hold_timeout_releases_and_requeues(self):
        self._register(qty=2)
        c1 = self._casualty(name="伤员1")
        c2 = self._casualty(name="伤员2")
        r1 = self.service.create_request(DISPATCH, {"casualty_id": c1["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 2})
        r2 = self.service.create_request(DISPATCH, {"casualty_id": c2["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 2})
        # 把锁库到期时间拨到过去，模拟超时
        from datetime import datetime, timezone
        past_ts = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.service.repository.transaction() as conn:
            conn.execute("UPDATE requests SET held_expires_at=? WHERE id=?", (past_ts, r1["id"]))
        result = self.service.sweep(BANK)
        self.assertEqual(len(result["released"]), 1)
        r1 = self.service.request_detail(DISPATCH, r1["id"])
        # r1超时被释放（旧占用released），又因FIFO队首立即重新锁库，r2继续等
        statuses = {a["status"] for a in r1["allocations"]}
        self.assertEqual(statuses, {"held", "released"})
        self.assertEqual(r1["state"], "held")
        reasons = [e["details"].get("reason") for e in r1["events"] if e["action"] == "released"]
        self.assertIn("锁库超时自动释放", reasons)
        r2 = self.service.request_detail(DISPATCH, r2["id"])
        self.assertEqual(r2["state"], "pending")

    def test_expired_batch_excluded_and_held_expiry_blocks_ship(self):
        hospital = self.service.register_hospital(BANK, {"name": "院", "distance_km": 4})
        with self.assertRaises(ValidationError):
            self.service.register_batch(BANK, hospital["id"], {"blood_type": "O+", "component": "红细胞",
                                                               "quantity": 2, "expires_on": past()})
        self.service.register_batch(BANK, hospital["id"], {"blood_type": "O+", "component": "红细胞",
                                                           "quantity": 2, "expires_on": future(1)})
        casualty = self._casualty()
        request = self.service.create_request(DISPATCH, {"casualty_id": casualty["id"], "blood_type": "O+",
                                                         "component": "红细胞", "quantity": 2})
        self.assertEqual(request["state"], "held")
        # 模拟效期越过：清扫释放
        from src import rules as rules_mod
        original = rules_mod.BloodRules.is_fresh
        rules_mod.BloodRules.is_fresh = lambda self, expires_on, today: False
        try:
            result = self.service.sweep(BANK)
            self.assertEqual(len(result["released"]), 1)
            with self.assertRaises(Conflict):
                self.service.ship(HOSP, request["id"], {})
        finally:
            rules_mod.BloodRules.is_fresh = original

    def test_persistence_trace_after_restart(self):
        _, batch = self._register(name="市一院", qty=5, batch_no="B-9001")
        casualty = self._casualty(name="可溯源伤员")
        request = self.service.create_request(DISPATCH, {"casualty_id": casualty["id"], "blood_type": "O+",
                                                         "component": "红细胞", "quantity": 3})
        self.service.ship(HOSP, request["id"], {})
        # 重启：用同一数据库新建整套服务
        restarted = build_service(self.db_path)
        trace = restarted.trace_casualty(DISPATCH, casualty["id"])
        self.assertEqual(trace["name"], "可溯源伤员")
        self.assertEqual(len(trace["requests"]), 1)
        shipped_request = trace["requests"][0]
        self.assertEqual(shipped_request["state"], "shipped")
        self.assertEqual(shipped_request["shipped_quantity"], 3)
        alloc = shipped_request["allocations"][0]
        self.assertEqual(alloc["batch_no"], "B-9001")
        self.assertEqual(alloc["status"], "shipped")
        self.assertEqual(alloc["hospital_name"], "市一院")
        actions = [e["action"] for e in shipped_request["events"]]
        self.assertEqual(actions, ["request_created", "held", "shipped"])
        # 库存扣减同样持久
        inv = {b["batch_no"]: b for b in restarted.list_batches(BANK)}
        self.assertEqual(inv["B-9001"]["quantity"], 2)

    def test_partial_ship_releases_remainder_to_queue(self):
        self._register(qty=3)
        c1 = self._casualty(name="伤员1")
        c2 = self._casualty(name="伤员2")
        r1 = self.service.create_request(DISPATCH, {"casualty_id": c1["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 3})
        r2 = self.service.create_request(DISPATCH, {"casualty_id": c2["id"], "blood_type": "O+",
                                                    "component": "红细胞", "quantity": 2})
        hold_line = r1["allocations"][0]
        # 只实发1袋，余下2袋释放给排队的r2
        self.service.ship(HOSP, r1["id"], {"items": [{"allocation_id": hold_line["id"], "quantity": 1}]})
        r2 = self.service.request_detail(DISPATCH, r2["id"])
        self.assertEqual(r2["state"], "held")
        self.assertEqual(r2["allocations"][0]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
