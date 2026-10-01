"""Shared hermetic fixtures for the log-interrogator test suite."""

import json
import os
import sqlite3
import tempfile

from loginterrogator.ingest import SCHEMA, signature_of

BASE_TS = 1_790_600_000  # fixed clock: 2026-09-29T00:00:00Z-ish, deterministic


def make_db(rows):
    """Build a temp SQLite DB from row dicts.

    Each row: {"ts": int, "level": str, "service": str, "msg": str,
               "attrs": dict (optional)}.
    Returns the db path. No wall clock, no randomness.
    """
    tmp = tempfile.mkdtemp(prefix="loginterrogator-test-")
    db = os.path.join(tmp, "test.db")
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        for r in rows:
            sig, template = signature_of(r["msg"])
            con.execute(
                "INSERT INTO logs(ts, level, service, msg, attrs, sig, template)"
                " VALUES (?,?,?,?,?,?,?)",
                (r["ts"], r["level"], r["service"], r["msg"],
                 json.dumps(r.get("attrs", {}), sort_keys=True), sig, template),
            )
        con.commit()
    finally:
        con.close()
    return db


def burst(ts_start, n, service="payments", level="ERROR", msg="boom"):
    """n identical log lines, one per second."""
    return [{"ts": ts_start + i, "level": level, "service": service, "msg": msg}
            for i in range(n)]


def append_rows(db, rows):
    """Insert row dicts (same shape as make_db) into an existing DB."""
    import json as _json
    con = sqlite3.connect(db)
    try:
        for r in rows:
            sig, template = signature_of(r["msg"])
            con.execute(
                "INSERT INTO logs(ts, level, service, msg, attrs, sig, template)"
                " VALUES (?,?,?,?,?,?,?)",
                (r["ts"], r["level"], r["service"], r["msg"],
                 _json.dumps(r.get("attrs", {}), sort_keys=True), sig, template),
            )
        con.commit()
    finally:
        con.close()
