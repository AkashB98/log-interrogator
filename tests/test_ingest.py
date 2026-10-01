"""Tests for ingest: schema, validation, malformed-line accounting, signatures."""

import json
import os
import sqlite3
import tempfile
import unittest

from loginterrogator.ingest import (
    ingest_jsonl, normalize_message, parse_ts, signature_of, validate_line, db_max_ts,
)
from .helpers import make_db, BASE_TS

GOOD = {"ts": "2026-09-29T10:00:00Z", "level": "error", "service": "payments",
        "msg": "charge failed for order ORD-1", "attrs": {"b": 2, "a": 1}}


def _write_jsonl(lines):
    tmp = tempfile.mkdtemp(prefix="ingest-test-")
    p = os.path.join(tmp, "in.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")
    return p, os.path.join(tmp, "out.db")


class TestIngestAccounting(unittest.TestCase):
    def test_malformed_and_invalid_counted_and_reported(self):
        lines = [
            json.dumps(GOOD),
            "{not json",
            "plain text, not json",
            json.dumps({"ts": "2026-09-29T10:00:00Z", "level": "INFO"}),  # missing svc/msg
            json.dumps({"ts": "bogus", "level": "INFO", "service": "a", "msg": "m"}),
            json.dumps({"ts": 1, "level": "NOPE", "service": "a", "msg": "m"}),
            json.dumps({"ts": 1, "level": "INFO", "service": "", "msg": "m"}),
            json.dumps({"ts": 1, "level": "INFO", "service": "a", "msg": ""}),
            json.dumps({"ts": 1, "level": "INFO", "service": "a", "msg": "m", "attrs": [1]}),
            json.dumps([1, 2]),
            json.dumps({"ts": True, "level": "INFO", "service": "a", "msg": "m"}),
            json.dumps({"level": "INFO", "service": "a", "msg": "m"}),
            "",  # blank lines are ignored, not counted
        ]
        p, db = _write_jsonl(lines)
        stats = ingest_jsonl(p, db)
        self.assertEqual(stats["lines"], 12)
        self.assertEqual(stats["ingested"], 1)
        self.assertEqual(stats["malformed"], 2)
        self.assertEqual(stats["invalid"], 9)
        self.assertEqual(stats["invalid_kinds"]["bad_ts"], 3)
        self.assertEqual(stats["invalid_kinds"]["bad_level"], 1)
        self.assertEqual(stats["invalid_kinds"]["bad_service"], 2)
        self.assertEqual(stats["invalid_kinds"]["bad_msg"], 1)
        self.assertEqual(stats["invalid_kinds"]["bad_attrs"], 1)
        self.assertEqual(stats["invalid_kinds"]["not_object"], 1)
        con = sqlite3.connect(db)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 1)
        finally:
            con.close()

    def test_levels_uppercased_and_attrs_canonical(self):
        p, db = _write_jsonl([json.dumps(GOOD)])
        ingest_jsonl(p, db)
        con = sqlite3.connect(db)
        try:
            row = con.execute("SELECT level, attrs FROM logs").fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], "ERROR")
        self.assertEqual(row[1], '{"a": 1, "b": 2}')

    def test_ts_iso_and_epoch_accepted(self):
        for ts in ("2026-09-29T10:00:00Z", "2026-09-29 10:00:00", 1790676000, 1790676000.9):
            row, err = validate_line({"ts": ts, "level": "INFO",
                                      "service": "auth", "msg": "m"})
            self.assertIsNone(err, ts)
            self.assertEqual(row["ts"], 1790676000)

    def test_ts_bool_and_garbage_rejected(self):
        for ts in (True, None, "yesterday", [1]):
            _, err = validate_line({"ts": ts, "level": "INFO",
                                    "service": "auth", "msg": "m"})
            self.assertEqual(err, "bad_ts")


class TestSignatures(unittest.TestCase):
    def test_numeric_variants_share_signature(self):
        s1, t1 = signature_of("order ORD-881231 failed after 300ms")
        s2, t2 = signature_of("order ORD-881232 failed after 1200ms")
        self.assertEqual(s1, s2)
        self.assertIn("<n>", t1)

    def test_different_templates_differ(self):
        self.assertNotEqual(signature_of("card declined")[0],
                            signature_of("card approved")[0])

    def test_normalize_uuid_ip_hex(self):
        t = normalize_message(
            "req a3f9c21b-8e04-4d2f-9a1b-7c3d5e6f8091 from 10.0.0.5 token 0xdeadbeef")
        self.assertIn("<uuid>", t)
        self.assertIn("<ip>", t)
        self.assertIn("<hex>", t)

    def test_long_plain_number_not_hex(self):
        # "30000" must become <n>, not <hex> (regression: the long-hex
        # pattern once swallowed plain long numbers)
        t = normalize_message("timeout after 30000ms")
        self.assertIn("timeout after <n>ms", t)
        self.assertNotIn("<hex>", t)

    def test_sig_is_16_hex_chars(self):
        sig, _ = signature_of("anything")
        self.assertRegex(sig, r"^[0-9a-f]{16}$")


class TestDbHelpers(unittest.TestCase):
    def test_db_max_ts(self):
        db = make_db([
            {"ts": BASE_TS, "level": "INFO", "service": "auth", "msg": "a"},
            {"ts": BASE_TS + 500, "level": "INFO", "service": "auth", "msg": "b"},
        ])
        self.assertEqual(db_max_ts(db), BASE_TS + 500)

    def test_db_max_ts_empty(self):
        self.assertIsNone(db_max_ts(make_db([])))

    def test_schema_indexes_exist(self):
        db = make_db([])
        con = sqlite3.connect(db)
        try:
            idx = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        finally:
            con.close()
        self.assertTrue({"idx_logs_ts", "idx_logs_svc_lvl", "idx_logs_sig"} <= idx)


if __name__ == "__main__":
    unittest.main()
