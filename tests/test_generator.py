"""Tests for the sample-data generator: determinism, shape, incident script."""

import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_sample import (  # noqa: E402
    build_lines, generate_events, NOW, INCIDENT_START, INCIDENT_END,
    INCIDENT_SIG, MALFORMED, SEED,
)


class TestGenerator(unittest.TestCase):
    def test_byte_identical_across_runs(self):
        self.assertEqual(build_lines(), build_lines())

    def test_seed_changes_output(self):
        self.assertNotEqual(build_lines(seed=SEED), build_lines(seed=SEED + 1))

    def test_malformed_line_count(self):
        lines = build_lines()
        bad = 0
        for ln in lines:
            try:
                json.loads(ln)
            except json.JSONDecodeError:
                bad += 1
        expect = 0
        for m in MALFORMED:
            try:
                json.loads(m)
            except json.JSONDecodeError:
                expect += 1
        self.assertEqual(bad, expect)

    def test_events_sorted_by_ts(self):
        events = generate_events()
        ts = [e["ts"] for e in events]
        self.assertEqual(ts, sorted(ts))

    def test_incident_signature_only_in_incident_window(self):
        events = generate_events()
        hits = [e for e in events if e["msg"] == INCIDENT_SIG]
        self.assertGreater(len(hits), 1000)
        for e in hits:
            ts = int(datetime.fromisoformat(
                e["ts"].replace("Z", "+00:00")).timestamp())
            self.assertGreaterEqual(ts, INCIDENT_START)
            self.assertLess(ts, INCIDENT_END)
        self.assertTrue(all(e["service"] == "payments" and e["level"] == "ERROR"
                            for e in hits))

    def test_window_spans_72h_ending_at_now(self):
        events = generate_events()
        first = events[0]["ts"]
        last = events[-1]["ts"]
        self.assertTrue(first.startswith("2026-09-26T18:"))
        self.assertTrue(last.startswith("2026-09-29T17:"))

    def test_line_count_in_range(self):
        self.assertTrue(5000 <= len(build_lines()) <= 15000)


if __name__ == "__main__":
    unittest.main()
