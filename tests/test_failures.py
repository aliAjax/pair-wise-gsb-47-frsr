"""失败场景：权限、重复编号、非法状态转换与角色边界。"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


BANK = Actor("banker", "blood_bank")
DISPATCH = Actor("coord", "dispatcher")
HOSP = Actor("hospital", "hospital")
OUTSIDER = Actor("x", "outsider")


def future(days=14):
    return (date.today() + timedelta(days=days)).isoformat()


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        hospital = self.service.register_hospital(BANK, {"name": "院", "distance_km": 5})
        self.service.register_batch(BANK, hospital["id"], {"blood_type": "O+", "component": "红细胞",
                                                          "quantity": 2, "expires_on": future()})
        casualty = self.service.register_casualty(DISPATCH, {"name": "伤", "blood_type": "O+"})
        return hospital, casualty

    def test_anonymous_denied(self):
        with self.assertRaises(PermissionDenied):
            self.service.list_requests(Actor("  ", "admin"))

    def test_role_boundaries(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_hospital(OUTSIDER, {"name": "院", "distance_km": 1})
        with self.assertRaises(PermissionDenied):
            self.service.register_casualty(HOSP, {"name": "伤", "blood_type": "O+"})

    def test_duplicate_codes(self):
        self.service.register_hospital(BANK, {"name": "院一", "distance_km": 1, "code": "H9"})
        with self.assertRaises(Conflict):
            self.service.register_hospital(BANK, {"name": "院二", "distance_km": 2, "code": "H9"})

    def test_ship_without_hold_rejected(self):
        _, casualty = self._seed()
        request = self.service.create_request(DISPATCH, {"casualty_id": casualty["id"], "blood_type": "O+",
                                                        "component": "红细胞", "quantity": 2})
        self.service.cancel(DISPATCH, request["id"], {"reason": "不要了"})
        with self.assertRaises(Conflict):
            self.service.ship(HOSP, request["id"], {})

    def test_ship_quantity_out_of_range(self):
        _, casualty = self._seed()
        request = self.service.create_request(DISPATCH, {"casualty_id": casualty["id"], "blood_type": "O+",
                                                        "component": "红细胞", "quantity": 2})
        line = request["allocations"][0]
        with self.assertRaises(ValidationError):
            self.service.ship(HOSP, request["id"], {"items": [{"allocation_id": line["id"], "quantity": 9}]})
        with self.assertRaises(ValidationError):
            self.service.ship(HOSP, request["id"], {"items": [{"allocation_id": line["id"], "quantity": 0}]})

    def test_cancel_requires_reason(self):
        _, casualty = self._seed()
        request = self.service.create_request(DISPATCH, {"casualty_id": casualty["id"], "blood_type": "O+",
                                                        "component": "红细胞", "quantity": 2})
        with self.assertRaises(ValidationError):
            self.service.cancel(DISPATCH, request["id"], {"reason": "  "})

    def test_unknown_casualty(self):
        with self.assertRaises(ValidationError):
            self.service.create_request(DISPATCH, {"casualty_id": "x", "blood_type": "O+",
                                                  "component": "红细胞", "quantity": 1})


if __name__ == "__main__":
    unittest.main()
