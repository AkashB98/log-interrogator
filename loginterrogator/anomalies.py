"""Anomaly detection over ingested logs: spikes, new signatures, level surges.

Fully deterministic: no randomness, no wall clock — the caller supplies the
scan window. Findings carry their evidence (counts, windows, sample messages)
so every flag is explainable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class SignatureEvidence:
    sig: str
    template: str
    count: int
    sample_msg: str


@dataclass
class Finding:
    kind: str  # "error_spike" | "new_signature" | "level_surge"
    service: str
    level: str  # level group, e.g. "ERROR/CRITICAL" or "WARN"
    scan_start: int
    scan_end: int
    baseline_start: int
    baseline_end: int
    baseline_count: int
    expected: float
    window_count: int
    multiplier: float
    signatures: list[SignatureEvidence] = field(default_factory=list)

    def headline(self) -> str:
        if self.kind == "new_signature":
            names = ", ".join(f'"{s.template}" ({s.count:,})' for s in self.signatures)
            return (f"[{self.service}] {len(self.signatures)} new "
                    f"{self.level} signature(s): {names}")
        return (f"[{self.service}] {self.level} spike: {self.window_count:,} in window "
                f"vs ~{self.expected:.1f} expected from baseline "
                f"({self.multiplier:.1f}x)")


def _top_sigs(con, start, end, service, levels, limit=3):
    q = ("SELECT sig, template, COUNT(*), MIN(msg) FROM logs "
         "WHERE ts >= ? AND ts < ? AND service = ? AND level IN (%s) "
         "GROUP BY sig, template ORDER BY COUNT(*) DESC, template ASC LIMIT %d"
         % (",".join("?" * len(levels)), limit))
    out = []
    for sig, template, n, sample in con.execute(q, (start, end, service, *levels)):
        out.append(SignatureEvidence(sig, template, n, sample))
    return out


def scan_anomalies(db_path: str, scan_start: int, scan_end: int,
                   baseline_hours: int = 24,
                   services: list[str] | None = None,
                   min_count: int = 15,
                   spike_factor: float = 4.0) -> list[Finding]:
    """Scan [scan_start, scan_end) against a trailing baseline window.

    baseline = [scan_start - baseline_hours, scan_start).
    A service/level-group is flagged when its scan-window count is at least
    `min_count` AND at least `spike_factor` x the baseline-implied expectation
    (scaled by window length).
    """
    from .query import SERVICES
    services = list(services or SERVICES)
    baseline_start = scan_start - baseline_hours * 3600
    scan_secs = scan_end - scan_start
    base_secs = scan_start - baseline_start

    groups = [("ERROR", ("ERROR", "CRITICAL")), ("WARN", ("WARN",))]
    con = sqlite3.connect(db_path)
    try:
        findings: list[Finding] = []
        for service in services:
            for label, levels in groups:
                bq = ("SELECT COUNT(*) FROM logs WHERE ts >= ? AND ts < ? "
                      "AND service = ? AND level IN (%s)"
                      % ",".join("?" * len(levels)))
                b = con.execute(bq, (baseline_start, scan_start, service, *levels)).fetchone()[0]
                w = con.execute(bq, (scan_start, scan_end, service, *levels)).fetchone()[0]
                expected = b * (scan_secs / base_secs) if base_secs else 0.0
                if w >= min_count and expected > 0 and w >= spike_factor * expected:
                    mult = w / expected
                    sigs = _top_sigs(con, scan_start, scan_end, service, levels)
                    findings.append(Finding(
                        kind="error_spike" if label == "ERROR" else "level_surge",
                        service=service, level="ERROR/CRITICAL" if label == "ERROR" else "WARN",
                        scan_start=scan_start, scan_end=scan_end,
                        baseline_start=baseline_start, baseline_end=scan_start,
                        baseline_count=b, expected=expected, window_count=w,
                        multiplier=mult, signatures=sigs))
        # new signatures: first seen *anywhere in history* inside the scan window,
        # >= 5 occurrences in the window. The first-seen subquery must look at
        # the whole table, not just the window's rows.
        nq = ("SELECT sig, template, COUNT(*), MIN(msg) FROM logs "
              "WHERE ts >= ? AND ts < ? AND level IN ('ERROR','CRITICAL') "
              "GROUP BY sig, template "
              "HAVING (SELECT MIN(ts) FROM logs l2 WHERE l2.sig = logs.sig) >= ? "
              "AND COUNT(*) >= 5")
        by_service: dict[str, list] = {}
        for sig, template, n, sample in con.execute(
                nq, (scan_start, scan_end, scan_start)):
            svc = con.execute(
                "SELECT service FROM logs WHERE sig = ? AND ts >= ? AND ts < ? "
                "GROUP BY service ORDER BY COUNT(*) DESC LIMIT 1",
                (sig, scan_start, scan_end)).fetchone()[0]
            by_service.setdefault(svc, []).append(
                SignatureEvidence(sig, template, n, sample))
        for svc in sorted(by_service):
            sigs = sorted(by_service[svc], key=lambda s: (-s.count, s.template))
            findings.append(Finding(
                kind="new_signature", service=svc, level="ERROR/CRITICAL",
                scan_start=scan_start, scan_end=scan_end,
                baseline_start=baseline_start, baseline_end=scan_start,
                baseline_count=0, expected=0.0, window_count=sum(s.count for s in sigs),
                multiplier=float("inf"), signatures=sigs))
    finally:
        con.close()
    rank = {"error_spike": 0, "new_signature": 1, "level_surge": 2}
    findings.sort(key=lambda f: (rank[f.kind], f.service))
    return findings
