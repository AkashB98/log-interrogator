"""One-command end-to-end demo: ingest sample logs -> ask 3 questions -> anomaly scan.

Fully offline, deterministic (seeded data, fixed clock). No keys, no network.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from loginterrogator import ingest_jsonl, ask, scan_anomalies  # noqa: E402
from loginterrogator.ingest import db_max_ts  # noqa: E402
from generate_sample import NOW, INCIDENT_START, INCIDENT_END  # noqa: E402

QUESTIONS = [
    "how many errors did payments log yesterday",
    "what are the top error signatures across all services in the last 24 hours",
    "any new error signatures in the last 48 hours",
]


def main() -> int:
    print("== log-interrogator demo (all data SIMULATED) ==\n")
    tmp = tempfile.mkdtemp(prefix="loginterrogator-demo-")
    db = os.path.join(tmp, "logs.db")

    print("[1/4] ingesting sample/logs.jsonl ...")
    stats = ingest_jsonl(os.path.join(HERE, "sample", "logs.jsonl"), db)
    print(f"      {stats['ingested']:,} ingested, {stats['malformed']} malformed, "
          f"{stats['invalid']} invalid (skipped, counted)")

    now = db_max_ts(db)
    print(f"      dataset 'now' = {now} (fixed clock, deterministic)\n")

    print("[2/4] asking 3 canned questions ...")
    for q in QUESTIONS:
        ans = ask(q, db, now=now)
        print(f"  Q: {q}")
        if ans.refused:
            print(f"  REFUSED: {ans.refusal_reason}")
        else:
            print(f"  A: {ans.summary}")
            for row in ans.rows[:5]:
                print(f"     {row}")
        print()

    print("[3/4] anomaly scan over the scripted incident window ...")
    findings = scan_anomalies(db, INCIDENT_START, INCIDENT_END, baseline_hours=24)
    for f in findings:
        print("  - " + f.headline())
    if not findings:
        print("  (no anomalies — unexpected on the incident window!)")
        return 1

    print("\n[4/4] anomaly scan over a quiet pre-incident window (expect none) ...")
    quiet = scan_anomalies(db, INCIDENT_START - 6 * 3600, INCIDENT_START,
                           baseline_hours=24)
    err_q = [f for f in quiet if f.kind == "error_spike"]
    print(f"  error spikes on quiet window: {len(err_q)} (expect 0)")
    if err_q:
        return 1

    print("\ndemo OK: incident flagged, quiet window clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
