"""CLI for log-interrogator.

    python3 cli.py ingest sample/logs.jsonl logs.db
    python3 cli.py ask "how many errors did payments log yesterday" --db logs.db
    python3 cli.py anomalies --db logs.db --scan-start 2026-09-28T06:00 --scan-end 2026-09-28T12:00
    python3 cli.py serve --db logs.db --port 8472   # JSON API on 127.0.0.1
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from loginterrogator import ingest_jsonl, ask, scan_anomalies, QueryEngine
from loginterrogator.ingest import db_max_ts


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt_arg(s: str) -> int:
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"cannot parse datetime: {s!r} (try 2026-09-28T06:00)")


def cmd_ingest(a):
    stats = ingest_jsonl(a.jsonl, a.db)
    print(json.dumps(stats, indent=2))


def _print_answer(ans):
    if ans.refused:
        print(f"REFUSED: {ans.refusal_reason}")
        return
    print(f"Q: {ans.question}")
    print(f"window: {ans.window_label}")
    print(ans.summary)
    if ans.rows and ans.intent not in ("count",):
        cols = ans.columns or [f"c{i}" for i in range(len(ans.rows[0]))]
        widths = [len(c) for c in cols]
        for r in ans.rows[:10]:
            for i, v in enumerate(r):
                widths[i] = max(widths[i], len(str(v)))
        print("  " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
        for r in ans.rows[:10]:
            print("  " + " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)))


def cmd_ask(a):
    ans = ask(a.question, a.db)
    _print_answer(ans)
    if ans.refused:
        sys.exit(2)


def cmd_anomalies(a):
    scan_start = _parse_dt_arg(a.scan_start) if a.scan_start else None
    scan_end = _parse_dt_arg(a.scan_end) if a.scan_end else None
    now = db_max_ts(a.db) or 0
    if scan_end is None:
        scan_end = now
    if scan_start is None:
        scan_start = scan_end - 6 * 3600
    findings = scan_anomalies(a.db, scan_start, scan_end,
                              baseline_hours=a.baseline_hours)
    print(f"scan window: {_iso(scan_start)} .. {_iso(scan_end)} "
          f"(baseline {a.baseline_hours}h trailing)")
    if not findings:
        print("No anomalies detected.")
        return
    for f in findings:
        print("- " + f.headline())
        for s in f.signatures[:3]:
            print(f'    sig={s.sig} n={s.count:,} template="{s.template}"')
            print(f'    sample: {s.sample_msg[:120]}')


def cmd_serve(a):
    db_path = a.db

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/ask":
                q = qs.get("q", [""])[0]
                ans = ask(q, db_path)
                self._send({"refused": ans.refused,
                            "refusal_reason": ans.refusal_reason,
                            "intent": ans.intent,
                            "window": ans.window_label,
                            "summary": ans.summary,
                            "columns": ans.columns,
                            "rows": [list(r) for r in ans.rows]})
            elif u.path == "/anomalies":
                now = db_max_ts(db_path) or 0
                end = int(qs.get("end", [now])[0])
                start = int(qs.get("start", [end - 6 * 3600])[0])
                fs = scan_anomalies(db_path, start, end)
                self._send([{"kind": f.kind, "service": f.service,
                             "level": f.level, "headline": f.headline(),
                             "window_count": f.window_count,
                             "expected": f.expected,
                             "multiplier": f.multiplier,
                             "signatures": [{"template": s.template,
                                             "count": s.count,
                                             "sample": s.sample_msg}
                                            for s in f.signatures]}
                            for f in fs])
            else:
                self._send({"error": "unknown path; try /ask?q=... or /anomalies"},
                           code=404)

    srv = HTTPServer(("127.0.0.1", a.port), H)
    print(f"serving log-interrogator API on 127.0.0.1:{a.port}  (/ask, /anomalies)")
    srv.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(prog="cli.py", description="log-interrogator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="ingest JSONL logs into SQLite")
    pi.add_argument("jsonl"); pi.add_argument("db")
    pi.set_defaults(fn=cmd_ingest)

    pa = sub.add_parser("ask", help="ask a natural-language question")
    pa.add_argument("question"); pa.add_argument("--db", default="logs.db")
    pa.set_defaults(fn=cmd_ask)

    pn = sub.add_parser("anomalies", help="scan a window for anomalies")
    pn.add_argument("--db", default="logs.db")
    pn.add_argument("--scan-start"); pn.add_argument("--scan-end")
    pn.add_argument("--baseline-hours", type=int, default=24)
    pn.set_defaults(fn=cmd_anomalies)

    ps = sub.add_parser("serve", help="tiny JSON API (demo only)")
    ps.add_argument("--db", default="logs.db")
    ps.add_argument("--port", type=int, default=8472)
    ps.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
