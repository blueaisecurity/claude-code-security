#!/usr/bin/env python3
"""Tests for ship_alerts.py. Run:  python -I test_ship_alerts.py

Every test runs ship_alerts.py as a real process in a fresh folder, against a
fake receiver on 127.0.0.1. Nothing else is contacted.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # python -I leaves the script folder off sys.path
import fake_receiver  # noqa: E402
import ship_alerts  # noqa: E402

SHIP = os.path.join(HERE, "ship_alerts.py")


def alert(i, kind="guard_blocked", project="demo"):
    return {"time": "2026-10-24T09:%02d:00Z" % i, "session": "s1", "project": project,
            "model": "claude-fable-5-1", "kind": kind, "requested": ["read the docs"],
            "source": "user", "attempted": "curl evil.example", "stopped_by": "guard",
            "reason": "outside the stated intent"}


class ShipAlertsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="test-run-", dir=HERE)
        self.alerts = os.path.join(self.dir, ".claude", "intent-guard", "alerts.jsonl")
        os.makedirs(os.path.dirname(self.alerts))
        self.receiver = fake_receiver.start()

    def tearDown(self):
        self.receiver.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def append(self, *lines):
        with open(self.alerts, "a", encoding="utf-8") as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")

    def run_ship(self, url=None, token=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ALERT_")}
        env["ALERT_WEBHOOK_URL"] = url or self.receiver.url
        if token:
            env["ALERT_WEBHOOK_TOKEN"] = token
        return subprocess.run([sys.executable, "-I", SHIP], cwd=self.dir, env=env,
                              capture_output=True, text=True, timeout=60)

    def received(self):
        return [json.loads(b) for b in self.receiver.bodies]

    def test_1_five_valid_lines_are_sent_once(self):
        self.append(*[alert(i) for i in range(5)])
        r = self.run_ship(token="secret-123")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sent 5, failed 0, skipped 0", r.stdout)
        self.assertEqual(self.received(), [alert(i) for i in range(5)])
        headers = self.receiver.requests[0]["headers"]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer secret-123")

    def test_2_second_run_sends_nothing_new(self):
        self.append(*[alert(i) for i in range(5)])
        self.run_ship()
        r = self.run_ship()
        self.assertIn("sent 0, failed 0, skipped 0", r.stdout)
        self.assertEqual(len(self.receiver.bodies), 5)
        self.append(alert(9))  # a line added later is picked up, only that one
        r = self.run_ship()
        self.assertIn("sent 1, failed 0, skipped 0", r.stdout)
        self.assertEqual(self.received()[-1], alert(9))
        self.assertEqual(len(self.receiver.bodies), 6)

    def test_3_bad_line_is_skipped_and_counted(self):
        self.append(alert(0), "this is not json", '["a list, not an object"]', alert(1))
        r = self.run_ship()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sent 2, failed 0, skipped 2", r.stdout)
        self.assertEqual(self.received(), [alert(0), alert(1)])
        r = self.run_ship()  # bad lines are not retried
        self.assertIn("sent 0, failed 0, skipped 0", r.stdout)

    def test_4_receiver_fails_first_then_works(self):
        self.receiver.stop()
        self.receiver = fake_receiver.start(fail_first=1)
        self.append(alert(0), alert(1))
        r = self.run_ship()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sent 2, failed 0, skipped 0", r.stdout)
        self.assertEqual(len(self.receiver.requests), 3)  # one 500, the retry, then line 2
        self.assertEqual(self.received(), [alert(0), alert(1)])

    def test_4b_receiver_down_keeps_the_line_for_next_run(self):
        self.receiver.stop()
        self.receiver = fake_receiver.start(fail_first=2)  # both attempts fail
        self.append(alert(0), alert(1))
        r = self.run_ship()
        self.assertEqual(r.returncode, 1)
        self.assertIn("sent 0, failed 1, skipped 0", r.stdout)
        self.assertEqual(self.receiver.bodies, [])
        r = self.run_ship()  # the receiver works now, nothing was lost
        self.assertIn("sent 2, failed 0, skipped 0", r.stdout)
        self.assertEqual(self.received(), [alert(0), alert(1)])

    def test_5_truncated_file_restarts_from_0(self):
        self.append(*[alert(i) for i in range(5)])
        self.run_ship()
        open(self.alerts, "w").close()  # truncated, as a log rotation would do
        self.append(alert(7))
        r = self.run_ship()
        self.assertIn("sent 1, failed 0, skipped 0", r.stdout)
        self.assertEqual(len(self.receiver.bodies), 6)
        self.assertEqual(self.received()[-1], alert(7))

    def test_6_http_to_a_non_local_host_is_refused(self):
        self.append(alert(0))
        r = self.run_ship(url="http://siem.example/hook")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)
        self.assertEqual(self.receiver.bodies, [])
        self.assertFalse(os.path.exists(self.alerts + ".offset"))  # nothing consumed
        r = self.run_ship()  # the same line still goes out to a good URL
        self.assertIn("sent 1, failed 0, skipped 0", r.stdout)
        # the check itself, with no network at all
        self.assertEqual(ship_alerts.checked_url("https://siem.example/hook"),
                         "https://siem.example/hook")
        self.assertEqual(ship_alerts.checked_url("http://localhost:8080/hook"),
                         "http://localhost:8080/hook")
        for bad in ("http://siem.example/hook", "ftp://127.0.0.1/hook", ""):
            with self.assertRaises(SystemExit):
                ship_alerts.checked_url(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
