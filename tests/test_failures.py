import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'hospital': 'North Hospital', 'casualty_count': 6, 'triage': 'yellow', 'required_beds': 8, 'required_ventilators': 2, 'hospital_beds': 24, 'hospital_ventilators': 6, 'transport_units': 5, 'transport_minutes': 18, 'reserve_ratio': 0.2}
FLOW = [('allocate', 'incident_commander', {}, 'allocated'), ('accept', 'hospital_liaison', {'liaison_acceptance': True, 'updated_available_beds': 12}, 'accepted'), ('transfer', 'transport_coordinator', {'vehicle_assigned': True}, 'transferred'), ('complete', 'hospital_liaison', {'outcome': 'treated', 'documentation_complete': True}, 'closed')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "SURGE-23001", CREATE_DATA)
        self.service.create(Actor("creator", "incident_commander"), "SURGE-23001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "incident_commander"), "SURGE-23001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "incident_commander"), "SURGE-23001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])
