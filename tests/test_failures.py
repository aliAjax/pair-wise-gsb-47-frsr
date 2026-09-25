import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
CLERK = Actor("clerk", "blood_bank_clerk")
LIAISON = Actor("liaison", "hospital_liaison")
COMMANDER = Actor("cmd", "incident_commander")


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def lot_data(blood_type="A+", quantity=2, hours=24):
    return {
        "hospital": "城北医院", "blood_type": blood_type, "component": "rbc",
        "expires_at": (NOW + timedelta(hours=hours)).isoformat(), "quantity": quantity,
    }


def request_data(casualty="CAS-1", blood_type="A+", units=2):
    return {"casualty_ref": casualty, "blood_type": blood_type, "component": "rbc", "units": units}


class ContentionTest(unittest.TestCase):
    """两家同时抢同一批：后提交的一方留待确认，原占用不动。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.service.register_lot(LIAISON, "LOT-1", lot_data(quantity=2))

    def tearDown(self):
        self.temp.cleanup()

    def test_second_request_goes_pending(self):
        first = self.service.create_request(COMMANDER, "REQ-1", request_data(casualty="CAS-1"))
        second = self.service.create_request(COMMANDER, "REQ-2", request_data(casualty="CAS-2"))
        first_alloc = first["allocations"][0]
        second_alloc = second["allocations"][0]
        self.assertEqual(first_alloc["state"], "reserved")
        self.assertEqual(second_alloc["state"], "pending")
        self.assertEqual(second_alloc["payload"]["lot_id"], first_alloc["payload"]["lot_id"])

        # 原占用不动，可调剂量不重复许诺
        lot = self.service.lot_detail(CLERK, first_alloc["payload"]["lot_id"])["lot"]
        self.assertEqual(lot["payload"]["quantity_available"], 0)
        self.assertEqual(lot["payload"]["quantity_reserved"], 2)

        # 库存不足时确认失败，继续留待确认
        with self.assertRaises(Conflict):
            self.service.allocation_action(CLERK, second_alloc["id"], "confirm", second_alloc["version"], {})

        # 先提交方取消后库存释放，后提交方才能确认转正
        self.service.allocation_action(LIAISON, first_alloc["id"], "cancel", first_alloc["version"], {})
        second_alloc = self.service.list_allocations(CLERK, casualty_ref="CAS-2")[0]
        confirmed = self.service.allocation_action(CLERK, second_alloc["id"], "confirm", second_alloc["version"], {})
        self.assertEqual(confirmed["state"], "reserved")
        lot = self.service.lot_detail(CLERK, first_alloc["payload"]["lot_id"])["lot"]
        self.assertEqual(lot["payload"]["quantity_available"], 0)
        self.assertEqual(lot["payload"]["quantity_reserved"], 2)


class TimeoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.service = Service(Repository(str(Path(self.temp.name) / "test.db")), DomainRules(),
                               hold_minutes=30, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def test_timeout_releases_hold(self):
        self.service.register_lot(LIAISON, "LOT-1", lot_data(quantity=2))
        detail = self.service.create_request(COMMANDER, "REQ-1", request_data())
        allocation = detail["allocations"][0]
        self.assertEqual(allocation["state"], "reserved")

        self.clock.advance(minutes=31)
        allocations = self.service.list_allocations(CLERK)
        self.assertEqual(allocations[0]["state"], "released")
        self.assertEqual(allocations[0]["payload"]["release_reason"], "timeout")
        lot = self.service.lot_detail(CLERK, allocation["payload"]["lot_id"])["lot"]
        self.assertEqual(lot["payload"]["quantity_available"], 2)
        self.assertEqual(lot["payload"]["quantity_reserved"], 0)


class ExpiryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.service = Service(Repository(str(Path(self.temp.name) / "test.db")), DomainRules(),
                               hold_minutes=30, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def test_expired_lot_never_candidate(self):
        self.service.register_lot(LIAISON, "LOT-OLD", lot_data(hours=-1))
        detail = self.service.create_request(COMMANDER, "REQ-1", request_data())
        self.assertEqual(detail["allocations"], [])
        self.assertEqual(detail["request"]["state"], "requested")

    def test_expired_lot_cannot_issue(self):
        self.service.register_lot(LIAISON, "LOT-1", lot_data(hours=1))
        detail = self.service.create_request(COMMANDER, "REQ-1", request_data())
        allocation = detail["allocations"][0]
        self.clock.advance(hours=2)
        # 清扫后批次过期、占用被释放，实发被拒绝
        lot = self.service.lot_detail(CLERK, allocation["payload"]["lot_id"])["lot"]
        self.assertEqual(lot["state"], "expired")
        with self.assertRaises(Conflict):
            self.service.allocation_action(LIAISON, allocation["id"], "issue", allocation["version"], {})


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_denied(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_lot(Actor("outsider", "outsider"), "LOT-1", lot_data())
        self.service.register_lot(LIAISON, "LOT-1", lot_data())
        detail = self.service.create_request(COMMANDER, "REQ-1", request_data())
        allocation = detail["allocations"][0]
        # 实发只能由医院联络员执行
        with self.assertRaises(PermissionDenied):
            self.service.allocation_action(CLERK, allocation["id"], "issue", allocation["version"], {})

    def test_duplicate_reference(self):
        self.service.register_lot(LIAISON, "LOT-1", lot_data())
        with self.assertRaises(Conflict):
            self.service.register_lot(LIAISON, "LOT-1", lot_data())

    def test_stale_version_rejected(self):
        self.service.register_lot(LIAISON, "LOT-1", lot_data())
        detail = self.service.create_request(COMMANDER, "REQ-1", request_data())
        allocation = detail["allocations"][0]
        with self.assertRaises(Conflict):
            self.service.allocation_action(LIAISON, allocation["id"], "issue", allocation["version"] + 1, {})

    def test_cancel_request_releases_allocations(self):
        self.service.register_lot(LIAISON, "LOT-1", lot_data())
        detail = self.service.create_request(COMMANDER, "REQ-1", request_data())
        request = detail["request"]
        cancelled = self.service.request_action(COMMANDER, request["id"], "cancel", request["version"], {})
        self.assertEqual(cancelled["request"]["state"], "cancelled")
        self.assertEqual(cancelled["allocations"][0]["state"], "released")
        lot = self.service.list_lots(CLERK)[0]
        self.assertEqual(lot["payload"]["quantity_available"], 2)


if __name__ == "__main__":
    unittest.main()
