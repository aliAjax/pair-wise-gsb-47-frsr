"""血型相容、效期与候选排序的纯规则测试。"""
import unittest
from datetime import date

from src.domain import ValidationError
from src.rules import BloodRules, ST_HELD, ST_PENDING


class CompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.rules = BloodRules()

    def test_rbc_compatibility(self):
        self.assertTrue(self.rules.compatible("红细胞", "O-", "AB+"))
        self.assertTrue(self.rules.compatible("红细胞", "O-", "O-"))
        self.assertTrue(self.rules.compatible("红细胞", "A+", "AB+"))
        self.assertFalse(self.rules.compatible("红细胞", "AB+", "O+"))
        self.assertFalse(self.rules.compatible("红细胞", "O+", "O-"))
        self.assertFalse(self.rules.compatible("红细胞", "A-", "B-"))

    def test_plasma_compatibility_is_reversed(self):
        # AB 血浆万能供血；O 型受者只能用 O/AB 血浆
        self.assertTrue(self.rules.compatible("血浆", "AB", "O"))
        self.assertTrue(self.rules.compatible("血浆", "A", "A"))
        self.assertFalse(self.rules.compatible("血浆", "A", "B"))
        self.assertFalse(self.rules.compatible("血浆", "O", "AB"))

    def test_expiry_freshness(self):
        self.assertTrue(self.rules.is_fresh("2026-09-25", date(2026, 9, 25)))
        self.assertFalse(self.rules.is_fresh("2026-09-24", date(2026, 9, 25)))

    def test_candidates_ordered_by_expiry_then_distance(self):
        request = {"component": "红细胞", "blood_type": "A+", "quantity": 3}
        batches = [
            {"id": 1, "component": "红细胞", "blood_type": "A+", "expires_on": "2026-10-01", "quantity": 2, "distance_km": 2},
            {"id": 2, "component": "红细胞", "blood_type": "O-", "expires_on": "2026-09-28", "quantity": 2, "distance_km": 50},
            {"id": 3, "component": "红细胞", "blood_type": "A+", "expires_on": "2026-09-20", "quantity": 2, "distance_km": 1},
        ]
        candidates = self.rules.candidate_batches(request, batches, {}, date(2026, 9, 25))
        # 过期批次3被排除；效期近的批次2排在批次1前（距离只在效期相同时决定）
        self.assertEqual([c["id"] for c in candidates], [2, 1])

    def test_plan_respects_held_quantity(self):
        request = {"component": "红细胞", "blood_type": "O+", "quantity": 3}
        batches = [
            {"id": 1, "component": "红细胞", "blood_type": "O+", "expires_on": "2026-10-01", "quantity": 3, "distance_km": 5},
        ]
        plan, satisfied = self.rules.plan_allocation(request, batches, {1: 2}, date(2026, 9, 25))
        self.assertEqual(plan, [{"batch_id": 1, "quantity": 1}])
        self.assertEqual(satisfied, 1)

    def test_wrong_component_excluded(self):
        request = {"component": "血浆", "blood_type": "AB", "quantity": 1}
        batches = [{"id": 1, "component": "红细胞", "blood_type": "AB", "expires_on": "2026-10-01", "quantity": 1, "distance_km": 1}]
        self.assertEqual(self.rules.candidate_batches(request, batches, {}, date(2026, 9, 25)), [])

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_batch({"blood_type": "X", "component": "红细胞", "quantity": 1, "expires_on": "2026-10-01"})
        with self.assertRaises(ValidationError):
            self.rules.validate_batch({"blood_type": "O-", "component": "红细胞", "quantity": 0, "expires_on": "2026-10-01"})
        with self.assertRaises(ValidationError):
            self.rules.validate_batch({"blood_type": "O-", "component": "红细胞", "quantity": 1, "expires_on": "bad"})


if __name__ == "__main__":
    unittest.main()
