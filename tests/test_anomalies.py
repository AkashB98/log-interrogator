"""Tests for anomaly detection: spikes, new signatures, surges, quiet data."""

import unittest

from loginterrogator.anomalies import scan_anomalies
from .helpers import make_db, burst, append_rows, BASE_TS

NOW = BASE_TS + 72 * 3600
SCAN_END = NOW
SCAN_START = NOW - 6 * 3600  # 6h scan window


def baseline_db(errors_per_hour=1, hours=30, service="payments", level="ERROR",
                msg="card declined"):
    """Steady baseline; last 6h are the scan window, rest is baseline.

    Anchored so the final `hours` end exactly at NOW (the scan window).
    """
    rows = []
    start = NOW - hours * 3600
    for h in range(hours):
        for _ in range(errors_per_hour):
            rows.append({"ts": start + h * 3600 + 7, "level": level,
                         "service": service, "msg": msg})
    return make_db(rows)


class TestSpikeDetection(unittest.TestCase):
    def test_spike_detected_with_evidence(self):
        db = baseline_db()
        # inject a spike: 60 errors in the scan window on top of baseline
        append_rows(db, burst(SCAN_START + 60, 60, msg="PaymentProcessor timeout"))

        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        spikes = [f for f in fs if f.kind == "error_spike"]
        self.assertEqual(len(spikes), 1)
        f = spikes[0]
        self.assertEqual(f.service, "payments")
        self.assertEqual(f.level, "ERROR/CRITICAL")
        self.assertGreater(f.multiplier, 4.0)
        self.assertGreaterEqual(f.window_count, 60)
        # evidence present
        self.assertTrue(f.signatures)
        self.assertIn("paymentprocessor timeout", f.signatures[0].template)
        self.assertIn("spike", f.headline())

    def test_no_spike_on_quiet_data(self):
        db = baseline_db(errors_per_hour=1)
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        self.assertEqual([f for f in fs if f.kind == "error_spike"], [])

    def test_min_count_gates_tiny_windows(self):
        # 11 errors in the scan window vs a small baseline: no error_spike
        # (min_count=15), and no new_signature ("card declined" pre-exists
        # in the baseline). Guards against noise on tiny samples.
        db = baseline_db(errors_per_hour=1)
        append_rows(db, burst(SCAN_START + 60, 5, msg="card declined"))
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        self.assertEqual(fs, [])

    def test_empty_db_no_findings(self):
        self.assertEqual(scan_anomalies(make_db([]), SCAN_START, SCAN_END), [])

    def test_zero_baseline_no_division_error(self):
        db = baseline_db(errors_per_hour=0)
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        self.assertEqual(fs, [])


class TestNewSignatures(unittest.TestCase):
    def test_new_signature_detected(self):
        db = baseline_db()
        append_rows(db, burst(SCAN_START + 60, 8, msg="brand new failure mode"))
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        news = [f for f in fs if f.kind == "new_signature"]
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0].signatures[0].template, "brand new failure mode")
        self.assertEqual(news[0].signatures[0].count, 8)

    def test_previously_seen_signature_not_new(self):
        # same message inside the scan window, but it first appeared days ago
        db = baseline_db(msg="card declined")  # baseline msg == scan msg
        append_rows(db, burst(SCAN_START + 60, 8, msg="card declined"))
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        self.assertEqual([f for f in fs if f.kind == "new_signature"], [])

    def test_below_occurrence_floor_not_reported(self):
        db = baseline_db(errors_per_hour=0)
        append_rows(db, burst(SCAN_START + 60, 4, msg="novel but rare"))
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        self.assertEqual([f for f in fs if f.kind == "new_signature"], [])


class TestSurgesAndOrdering(unittest.TestCase):
    def test_warn_surge_detected(self):
        rows = []
        start = NOW - 30 * 3600
        for h in range(30):
            rows.append({"ts": start + h * 3600, "level": "WARN",
                         "service": "inventory", "msg": "low stock"})
        rows += burst(SCAN_START + 60, 60, service="inventory",
                      level="WARN", msg="warehouse api flapping")
        db = make_db(rows)
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
        surges = [f for f in fs if f.kind == "level_surge"]
        self.assertEqual(len(surges), 1)
        self.assertEqual(surges[0].level, "WARN")

    def test_findings_sorted_deterministically(self):
        rows = []
        start = NOW - 30 * 3600
        for h in range(30):
            rows.append({"ts": start + h * 3600, "level": "ERROR",
                         "service": "orders", "msg": "old problem"})
            rows.append({"ts": start + h * 3600, "level": "WARN",
                         "service": "orders", "msg": "old warn"})
        rows += burst(SCAN_START + 60, 60, service="orders", msg="new explosion")
        rows += burst(SCAN_START + 120, 60, service="orders",
                      level="WARN", msg="warn storm")
        db = make_db(rows)
        twice = [scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=24)
                 for _ in range(2)]
        keys = lambda fs: [(f.kind, f.service) for f in fs]
        self.assertEqual(keys(twice[0]), keys(twice[1]))
        kinds = [f.kind for f in twice[0]]
        self.assertEqual(kinds, sorted(kinds,
                                       key=lambda k: {"error_spike": 0,
                                                      "new_signature": 1,
                                                      "level_surge": 2}[k]))

    def test_custom_baseline_hours(self):
        db = baseline_db(errors_per_hour=1)
        fs = scan_anomalies(db, SCAN_START, SCAN_END, baseline_hours=6)
        self.assertEqual(fs, [])


if __name__ == "__main__":
    unittest.main()
