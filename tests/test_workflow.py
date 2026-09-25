import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'hospital': 'North Hospital', 'casualty_count': 6, 'triage': 'yellow', 'required_beds': 8, 'required_ventilators': 2, 'hospital_beds': 24, 'hospital_ventilators': 6, 'transport_units': 5, 'transport_minutes': 18, 'reserve_ratio': 0.2}
FLOW = [('allocate', 'incident_commander', {}, 'allocated'), ('accept', 'hospital_liaison', {'liaison_acceptance': True, 'updated_available_beds': 12}, 'accepted'), ('transfer', 'transport_coordinator', {'vehicle_assigned': True}, 'transferred'), ('complete', 'hospital_liaison', {'outcome': 'treated', 'documentation_complete': True}, 'closed')]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "incident_commander"), "SURGE-23001", CREATE_DATA)
        self.assertEqual(record["state"], "reported")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "incident_commander"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])
