import unittest
from datetime import datetime, timedelta, timezone

from src.domain import ValidationError
from src.rules import DomainRules, compatible


NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def make_lot(lot_id, blood_type, component="rbc", expires_in_hours=24, available=2,
             hospital="城北医院", lat=None, lng=None, state="registered"):
    expires = (NOW + timedelta(hours=expires_in_hours)).isoformat()
    return {
        "id": lot_id, "kind": "blood_lot", "reference": "LOT-%d" % lot_id, "state": state, "version": 1,
        "payload": {
            "hospital": hospital, "blood_type": blood_type, "component": component, "expires_at": expires,
            "quantity_total": available, "quantity_available": available,
            "quantity_reserved": 0, "quantity_issued": 0,
            "location": {"lat": lat, "lng": lng} if lat is not None else None,
        },
    }


REQUEST = {"casualty_ref": "CAS-1", "blood_type": "A+", "component": "rbc", "units_needed": 4, "location": None}


class CompatibilityTest(unittest.TestCase):
    def test_rbc_compatibility(self):
        for recipient in ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]:
            self.assertTrue(compatible("rbc", "O-", recipient))
        for donor in ["A+", "A-", "O+", "O-"]:
            self.assertTrue(compatible("rbc", donor, "A+"))
        self.assertFalse(compatible("rbc", "B+", "A+"))
        self.assertFalse(compatible("rbc", "AB-", "A-"))

    def test_plasma_is_reversed(self):
        for recipient in ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]:
            self.assertTrue(compatible("plasma", "AB-", recipient))
        self.assertTrue(compatible("plasma", "O+", "O+"))
        self.assertFalse(compatible("plasma", "O+", "A+"))

    def test_platelets_require_same_type(self):
        self.assertTrue(compatible("platelets", "A+", "A+"))
        self.assertFalse(compatible("platelets", "O-", "A+"))


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_expired_and_incompatible_excluded(self):
        lots = [
            make_lot(1, "A+", expires_in_hours=-1),          # 已过期
            make_lot(2, "B+", expires_in_hours=24),          # 血型不相容
            make_lot(3, "A+", component="plasma"),           # 成分不符
            make_lot(4, "O-", expires_in_hours=24),          # 合格
        ]
        ranked = self.rules.rank_candidates(lots, REQUEST, NOW)
        self.assertEqual([lot["id"] for lot in ranked], [4])

    def test_ranking_by_expiry_then_distance(self):
        lots = [
            make_lot(1, "A+", expires_in_hours=48, lat=30.66, lng=104.06),
            make_lot(2, "A+", expires_in_hours=12, lat=30.66, lng=104.60),
            make_lot(3, "A+", expires_in_hours=12, lat=30.67, lng=104.07),
        ]
        request = dict(REQUEST, location={"lat": 30.67, "lng": 104.07})
        ranked = self.rules.rank_candidates(lots, request, NOW)
        self.assertEqual([lot["id"] for lot in ranked], [3, 2, 1])

    def test_plan_pending_when_short(self):
        lots = [make_lot(1, "A+", available=2)]
        plan = self.rules.plan_allocations(REQUEST, lots, NOW, units_open=4)
        self.assertEqual([(p["lot"]["id"], p["units"], p["status"]) for p in plan],
                         [(1, 2, "reserved"), (1, 2, "pending")])

    def test_plan_empty_without_candidates(self):
        lots = [make_lot(1, "B+")]
        self.assertEqual(self.rules.plan_allocations(REQUEST, lots, NOW, units_open=4), [])


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_lot(self):
        prepared = self.rules.prepare_lot({
            "hospital": "城北医院", "blood_type": "O-", "component": "rbc",
            "expires_at": "2026-09-27T08:00:00Z", "quantity": 3, "lat": 30.66, "lng": 104.06,
        })
        self.assertEqual(prepared["quantity_available"], 3)
        self.assertEqual(prepared["quantity_reserved"], 0)
        self.assertEqual(prepared["location"], {"lat": 30.66, "lng": 104.06})

    def test_invalid_blood_type_rejected(self):
        with self.assertRaises(ValidationError):
            self.rules.prepare_lot({
                "hospital": "城北医院", "blood_type": "C+", "component": "rbc",
                "expires_at": "2026-09-27T08:00:00Z", "quantity": 1,
            })

    def test_invalid_expiry_rejected(self):
        with self.assertRaises(ValidationError):
            self.rules.prepare_lot({
                "hospital": "城北医院", "blood_type": "O-", "component": "rbc",
                "expires_at": "not-a-date", "quantity": 1,
            })


if __name__ == "__main__":
    unittest.main()
