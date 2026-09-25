import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
CLERK = Actor("clerk", "blood_bank_clerk")
LIAISON = Actor("liaison", "hospital_liaison")
COMMANDER = Actor("cmd", "incident_commander")


def lot_data(hospital, blood_type, quantity, hours, lat, lng):
    return {
        "hospital": hospital, "blood_type": blood_type, "component": "rbc",
        "expires_at": (NOW + timedelta(hours=hours)).isoformat(), "quantity": quantity,
        "lat": lat, "lng": lng,
    }


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_full_flow_and_restart_trail(self):
        # 两家医院登记血袋：南城医院效期更近、距离更近，应优先被匹配
        self.service.register_lot(LIAISON, "LOT-N", lot_data("城北医院", "O+", 4, 48, 30.66, 104.06))
        self.service.register_lot(LIAISON, "LOT-S", lot_data("南城医院", "A+", 2, 12, 30.67, 104.07))

        detail = self.service.create_request(COMMANDER, "REQ-1", {
            "casualty_ref": "CAS-301", "blood_type": "A+", "component": "rbc",
            "units": 5, "lat": 30.67, "lng": 104.07,
        })
        request = detail["request"]
        self.assertEqual(request["state"], "matched")
        self.assertEqual(request["payload"]["coverage"], "full")
        allocations = sorted(detail["allocations"], key=lambda a: a["reference"])
        self.assertEqual([(a["state"], a["payload"]["units"]) for a in allocations],
                         [("reserved", 2), ("reserved", 3)])
        by_hospital = {a["payload"]["hospital"]: a for a in allocations}
        self.assertEqual(by_hospital["南城医院"]["payload"]["units"], 2)

        # 实发前：库存只是被预留，尚未扣减
        lots = {lot["reference"]: lot for lot in self.service.list_lots(CLERK)}
        self.assertEqual(lots["LOT-S"]["payload"]["quantity_issued"], 0)
        self.assertEqual(lots["LOT-S"]["payload"]["quantity_available"], 0)

        # 医院实发后才真正扣库存
        for allocation in allocations:
            self.service.allocation_action(LIAISON, allocation["id"], "issue", allocation["version"], {})
        lots = {lot["reference"]: lot for lot in self.service.list_lots(CLERK)}
        self.assertEqual(lots["LOT-S"]["payload"]["quantity_issued"], 2)
        self.assertEqual(lots["LOT-N"]["payload"]["quantity_issued"], 3)
        detail = self.service.request_detail(CLERK, request["id"])
        self.assertEqual(detail["request"]["state"], "fulfilled")

        # 重启后仍能沿伤员查到调剂去向
        restarted = build_service(self.db_path)
        trail = restarted.casualty_trail(CLERK, "CAS-301")
        self.assertEqual(trail["casualty_ref"], "CAS-301")
        self.assertEqual(len(trail["requests"]), 1)
        entry = trail["requests"][0]
        self.assertEqual(entry["request"]["state"], "fulfilled")
        destinations = {item["lot"]["hospital"]: item["allocation"]["state"] for item in entry["allocations"]}
        self.assertEqual(destinations, {"城北医院": "issued", "南城医院": "issued"})
        self.assertTrue(all(item["timeline"] for item in entry["allocations"]))


if __name__ == "__main__":
    unittest.main()
