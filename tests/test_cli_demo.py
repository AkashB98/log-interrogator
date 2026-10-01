"""Smoke tests: CLI subcommands and the end-to-end demo."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "cli.py", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def fresh_sample_db():
    tmp = tempfile.mkdtemp(prefix="cli-test-")
    db = os.path.join(tmp, "logs.db")
    r = run_cli("ingest", os.path.join(ROOT, "sample", "logs.jsonl"), db)
    assert r.returncode == 0, r.stderr
    return db


class TestCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = fresh_sample_db()

    def test_ingest_reports_stats(self):
        tmp = tempfile.mkdtemp(prefix="cli-test-")
        db = os.path.join(tmp, "x.db")
        r = run_cli("ingest", os.path.join(ROOT, "sample", "logs.jsonl"), db)
        self.assertEqual(r.returncode, 0, r.stderr)
        stats = json.loads(r.stdout)
        self.assertEqual(stats["ingested"], 14649)
        self.assertEqual(stats["malformed"], 2)
        self.assertEqual(stats["invalid"], 10)

    def test_ask_prints_answer(self):
        r = run_cli("ask", "how many errors did payments log yesterday",
                    "--db", self.db)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2,122", r.stdout)

    def test_ask_refusal_exit_code(self):
        r = run_cli("ask", "what is the weather", "--db", self.db)
        self.assertEqual(r.returncode, 2)
        self.assertIn("REFUSED", r.stdout)

    def test_anomalies_flags_incident(self):
        r = run_cli("anomalies", "--db", self.db,
                    "--scan-start", "2026-09-28T06:00",
                    "--scan-end", "2026-09-28T12:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("payments", r.stdout)
        self.assertIn("spike", r.stdout)
        self.assertIn("paymentprocessor timeout", r.stdout)

    def test_anomalies_quiet_window_clean(self):
        r = run_cli("anomalies", "--db", self.db,
                    "--scan-start", "2026-09-28T00:00",
                    "--scan-end", "2026-09-28T06:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No anomalies detected", r.stdout)


class TestDemo(unittest.TestCase):
    def test_demo_end_to_end(self):
        r = subprocess.run([sys.executable, "demo.py"], cwd=ROOT,
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("demo OK", r.stdout)
        self.assertIn("ERROR/CRITICAL spike", r.stdout)

    def test_demo_deterministic_output(self):
        outs = []
        for _ in range(2):
            r = subprocess.run([sys.executable, "demo.py"], cwd=ROOT,
                               capture_output=True, text=True, timeout=180)
            self.assertEqual(r.returncode, 0)
            outs.append(r.stdout)
        self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
