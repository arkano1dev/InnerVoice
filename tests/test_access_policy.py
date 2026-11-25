import hashlib
from pathlib import Path
import secrets
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
from access import authorize


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.token = secrets.token_urlsafe(32)
        self.digest = hashlib.sha256(self.token.encode()).hexdigest()

    def test_scoped_agent(self):
        clients = {"agent": {"digest": self.digest, "scope":"jobs", "peer":"192.0.2.1"}}
        def result(path, peer="192.0.2.1"):
            return authorize(clients, self.token, peer, path)
        self.assertEqual(result("/v1/capabilities"), ("agent", "jobs", None))
        self.assertEqual(result("/v1/jobs/example"), ("agent", "jobs", None))
        self.assertEqual(result("/v1/sessions")[2], "scope_denied")
        self.assertEqual(result("/v1/jobs", "192.0.2.2")[2], "peer_denied")
        self.assertEqual(authorize(clients, "other", "192.0.2.1", "/v1/jobs")[2], "unauthorized")

    def test_legacy_registration(self):
        self.assertEqual(authorize({"desktop": self.digest}, self.token, "192.0.2.1", "/v1/sessions"),
                         ("desktop", "both", None))
