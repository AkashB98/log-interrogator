"""JSONL -> SQLite ingestion for log-interrogator.

Every log line becomes one row:

    logs(id, ts, level, service, msg, attrs, sig, template)

- ts: unix epoch seconds (ingest accepts ISO-8601 strings or epoch numbers)
- attrs: canonical JSON of the per-line attribute dict (sorted keys)
- sig: sha256 hex[:16] of the normalized message *template*
- template: the normalized message text (numbers/ids replaced by placeholders)

Malformed lines (bad JSON, missing/invalid fields) are never silently dropped:
they are counted, skipped, and reported in the returned stats dict.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs(
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    level    TEXT    NOT NULL,
    service  TEXT    NOT NULL,
    msg      TEXT    NOT NULL,
    attrs    TEXT    NOT NULL DEFAULT '{}',
    sig      TEXT    NOT NULL,
    template TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_svc_lvl ON logs(service, level);
CREATE INDEX IF NOT EXISTS idx_logs_sig ON logs(sig);
"""

LEVELS = {"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_HEX = re.compile(r"\b0x[0-9a-f]+\b")
_LONGHEX = re.compile(r"\b[0-9a-f]{8,}\b")
_FLOAT = re.compile(r"\d+\.\d+")
_INT = re.compile(r"\d+")


def normalize_message(msg: str) -> str:
    """Collapse a raw log message into its signature template.

    Numbers, UUIDs, IPs and hex tokens become placeholders so that
    "order ORD-881231 failed" and "order ORD-881232 failed" share one
    signature while genuinely different messages do not.
    """
    m = msg.lower()
    m = _UUID.sub("<uuid>", m)
    m = _IP.sub("<ip>", m)
    m = _HEX.sub("<hex>", m)
    # Only treat a long hex-looking token as hex if it has an a-f letter,
    # so plain long numbers ("30000") are not swallowed here.
    m = _LONGHEX.sub(lambda t: "<hex>" if re.search(r"[a-f]", t.group(0)) else t.group(0), m)
    m = _FLOAT.sub("<n>", m)
    m = _INT.sub("<n>", m)
    m = re.sub(r"\s+", " ", m).strip()
    return m


def signature_of(msg: str) -> tuple[str, str]:
    """Return (sig, template) for a raw message."""
    template = normalize_message(msg)
    sig = hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]
    return sig, template


def parse_ts(value) -> int | None:
    """Parse an ISO-8601 string or epoch number to unix seconds; None if bad."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def validate_line(obj) -> tuple[dict | None, str | None]:
    """Validate a decoded JSON object. Returns (row_dict, error_kind)."""
    if not isinstance(obj, dict):
        return None, "not_object"
    ts = parse_ts(obj.get("ts"))
    level = obj.get("level")
    service = obj.get("service")
    msg = obj.get("msg")
    if ts is None:
        return None, "bad_ts"
    if not isinstance(level, str) or level.upper() not in LEVELS:
        return None, "bad_level"
    if not isinstance(service, str) or not service.strip():
        return None, "bad_service"
    if not isinstance(msg, str) or not msg.strip():
        return None, "bad_msg"
    attrs = obj.get("attrs", {})
    if not isinstance(attrs, dict):
        return None, "bad_attrs"
    sig, template = signature_of(msg)
    return {
        "ts": ts,
        "level": level.upper(),
        "service": service.strip(),
        "msg": msg,
        "attrs": json.dumps(attrs, sort_keys=True),
        "sig": sig,
        "template": template,
    }, None


def ingest_jsonl(jsonl_path: str, db_path: str, batch: int = 2000) -> dict:
    """Ingest a JSONL log file into SQLite. Returns stats.

    stats: {lines, ingested, malformed, invalid, invalid_kinds}
    - malformed: line was not valid JSON
    - invalid: valid JSON but failed schema validation (kind counted)
    """
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        stats = {"lines": 0, "ingested": 0, "malformed": 0, "invalid": 0,
                 "invalid_kinds": {}}
        rows: list[tuple] = []

        def flush():
            if rows:
                con.executemany(
                    "INSERT INTO logs(ts, level, service, msg, attrs, sig, template)"
                    " VALUES (?,?,?,?,?,?,?)",
                    rows,
                )
                rows.clear()

        with open(jsonl_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                stats["lines"] += 1
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    stats["malformed"] += 1
                    continue
                row, err = validate_line(obj)
                if err:
                    stats["invalid"] += 1
                    stats["invalid_kinds"][err] = stats["invalid_kinds"].get(err, 0) + 1
                    continue
                rows.append((row["ts"], row["level"], row["service"], row["msg"],
                             row["attrs"], row["sig"], row["template"]))
                stats["ingested"] += 1
                if len(rows) >= batch:
                    flush()
        flush()
        con.commit()
    finally:
        con.close()
    return stats


def db_max_ts(db_path: str) -> int | None:
    """Return the maximum ts in the logs table (the dataset's 'now')."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT MAX(ts) FROM logs").fetchone()
        return row[0] if row and row[0] is not None else None
    finally:
        con.close()
