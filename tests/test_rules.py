import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'hospital': 'North Hospital', 'casualty_count': 6, 'triage': 'yellow', 'required_beds': 8, 'required_ventilators': 2, 'hospital_beds': 24, 'hospital_ventilators': 6, 'transport_units': 5, 'transport_minutes': 18, 'reserve_ratio': 0.2}
FLOW = [('allocate', 'incident_commander', {}, 'allocated'), ('accept', 'hospital_liaison', {'liaison_acceptance': True, 'updated_available_beds': 12}, 'accepted'), ('transfer', 'transport_coordinator', {'vehicle_assigned': True}, 'transferred'), ('complete', 'hospital_liaison', {'outcome': 'treated', 'documentation_complete': True}, 'closed')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["demand_points"], 12)
        self.assertEqual(prepared["required_ambulances"], 3)
        self.assertTrue(prepared["capacity_ok"])

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertTrue(payload["allocation_confirmed"])

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["triage"] = 'blue'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)
