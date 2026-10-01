"""Deterministic sample-log generator.

Produces sample/logs.jsonl: ~72h of fictional JSONL logs for the made-up
"Helios Home" storefront (services: api-gateway, orders, inventory, auth,
payments), including one scripted incident window (payments 5xx-style spike
with a brand-new error signature) and a handful of malformed lines.

Everything is seeded (seed=42) and anchored to a fixed NOW, so re-running
produces byte-identical output. ALL DATA IS FICTIONAL / SIMULATED.
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timezone

NOW = int(datetime(2026, 9, 29, 18, 0, 0, tzinfo=timezone.utc).timestamp())
HOURS = 72
SEED = 42
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample", "logs.jsonl")

# Incident: payments outage, 30-36h before NOW (2026-09-28 06:00-12:00 UTC).
INCIDENT_START = NOW - 36 * 3600
INCIDENT_END = NOW - 30 * 3600
INCIDENT_ERR_PER_MIN = 6.0
INCIDENT_WARN_PER_MIN = 1.5

BASE_RATES = {  # mean log lines per minute, per (service, level)
    "api-gateway": {"INFO": 1.00, "WARN": 0.06, "ERROR": 0.012},
    "orders":      {"INFO": 0.50, "WARN": 0.03, "ERROR": 0.008},
    "inventory":   {"INFO": 0.35, "WARN": 0.05, "ERROR": 0.005},
    "auth":        {"INFO": 0.28, "WARN": 0.02, "ERROR": 0.004, "CRITICAL": 0.001},
    "payments":    {"INFO": 0.42, "WARN": 0.04, "ERROR": 0.012},
}

INCIDENT_SIG = "PaymentProcessor timeout after 30000ms (gateway=helios-pay)"
INCIDENT_WARN_TMPL = "payment attempt 3/3 failed for order {oid}, retrying in 60s"


def _poisson(rng: random.Random, mean: float) -> int:
    if mean <= 0:
        return 0
    l = math.exp(-mean)
    k, p = 0, 1.0
    while p > l:
        k += 1
        p *= rng.random()
    return k - 1


def _oid(rng): return "ORD-%06d" % rng.randint(1, 999999)
def _uid(rng): return "u_%06d" % rng.randint(1, 99999)
def _sku(rng): return "SKU-%05d" % rng.randint(1, 49999)
def _ip(rng): return ".".join(str(rng.randint(1, 254)) for _ in range(4))
def _rid(rng): return "%08x-%04x-%04x-%04x-%012x" % (
    rng.getrandbits(32), rng.getrandbits(16), rng.getrandbits(16),
    rng.getrandbits(16), rng.getrandbits(48))

TEMPLATES = {
    ("api-gateway", "INFO"): [
        lambda r: ("GET /api/v2/products 200 in %dms" % r.randint(8, 400),
                   {"request_id": _rid(r), "latency_ms": r.randint(8, 400)}),
        lambda r: ("POST /api/v2/orders 201 in %dms" % r.randint(20, 900),
                   {"request_id": _rid(r), "latency_ms": r.randint(20, 900)}),
        lambda r: ("GET /api/v2/inventory 200 in %dms" % r.randint(5, 250),
                   {"request_id": _rid(r), "latency_ms": r.randint(5, 250)}),
    ],
    ("api-gateway", "WARN"): [
        lambda r: ("rate limit near threshold for client %s (%d%% of quota)"
                   % (_rid(r)[:8], r.randint(80, 99)), {"client": _rid(r)[:8]}),
        lambda r: ("slow upstream: %s responded in %dms"
                   % (r.choice(["orders", "inventory", "auth"]), r.randint(1200, 4500)),
                   {"upstream": "orders"}),
    ],
    ("api-gateway", "ERROR"): [
        lambda r: ("upstream 502 from %s after %dms"
                   % (r.choice(["orders", "inventory"]), r.randint(2000, 8000)),
                   {"upstream": "orders"}),
        lambda r: ("request validation failed: missing field '%s'"
                   % r.choice(["email", "zip", "sku", "qty"]), {}),
    ],
    ("orders", "INFO"): [
        lambda r: ("order %s created for user %s ($%.2f)"
                   % (_oid(r), _uid(r), r.uniform(12, 890)),
                   {"order_id": _oid(r), "user": _uid(r)}),
        lambda r: ("order %s shipped via %s"
                   % (_oid(r), r.choice(["ups", "fedex", "usps"])), {"order_id": _oid(r)}),
    ],
    ("orders", "WARN"): [
        lambda r: ("order %s delayed: inventory hold pending" % _oid(r), {}),
        lambda r: ("duplicate webhook for order %s ignored" % _oid(r), {}),
    ],
    ("orders", "ERROR"): [
        lambda r: ("order %s failed: payment authorization declined" % _oid(r), {}),
        lambda r: ("order %s failed: address validation error" % _oid(r), {}),
    ],
    ("inventory", "INFO"): [
        lambda r: ("stock check sku %s: %d units" % (_sku(r), r.randint(0, 900)),
                   {"sku": _sku(r)}),
        lambda r: ("sku %s restocked (+%d units)" % (_sku(r), r.randint(20, 400)), {}),
    ],
    ("inventory", "WARN"): [
        lambda r: ("low stock alert sku %s: %d units remaining" % (_sku(r), r.randint(1, 9)), {}),
        lambda r: ("sku %s below reorder point" % _sku(r), {}),
    ],
    ("inventory", "ERROR"): [
        lambda r: ("inventory reservation failed for sku %s: insufficient stock" % _sku(r), {}),
    ],
    ("auth", "INFO"): [
        lambda r: ("user %s login success from %s" % (_uid(r), _ip(r)),
                   {"user": _uid(r), "ip": _ip(r)}),
        lambda r: ("token refreshed for user %s" % _uid(r), {}),
    ],
    ("auth", "WARN"): [
        lambda r: ("multiple failed logins for user %s (%d attempts)" % (_uid(r), r.randint(3, 8)), {}),
        lambda r: ("password reset requested for user %s" % _uid(r), {}),
    ],
    ("auth", "ERROR"): [
        lambda r: ("login failed for user %s: invalid credentials" % _uid(r), {}),
        lambda r: ("session expired mid-request for user %s" % _uid(r), {}),
    ],
    ("auth", "CRITICAL"): [
        lambda r: ("auth signing key rotation overdue", {}),
    ],
    ("payments", "INFO"): [
        lambda r: ("charge $%.2f succeeded for order %s" % (r.uniform(9, 1200), _oid(r)),
                   {"order_id": _oid(r)}),
        lambda r: ("refund $%.2f issued for order %s" % (r.uniform(9, 400), _oid(r)), {}),
    ],
    ("payments", "WARN"): [
        lambda r: ("payment retry 1/3 for order %s" % _oid(r), {}),
        lambda r: ("webhook delivery delayed for charge %s" % _rid(r)[:12], {}),
    ],
    ("payments", "ERROR"): [
        lambda r: ("card declined by issuer (code=do_not_honor)", {"code": "do_not_honor"}),
        lambda r: ("charge $%.2f failed: AVS mismatch" % r.uniform(9, 900), {}),
    ],
}

MALFORMED = [
    "{this is not json",
    "definitely not a log line",
    '{"ts": "2026-09-28T06:00:00Z", "level": "ERROR"}',                    # missing service/msg
    '{"ts": "not-a-time", "level": "INFO", "service": "auth", "msg": "x"}',  # bad ts
    '{"ts": 1790600000, "level": "NOPE", "service": "auth", "msg": "x"}',    # bad level
    '{"ts": 1790600000, "level": "INFO", "service": "", "msg": "x"}',        # bad service
    '{"ts": 1790600000, "level": "INFO", "service": "auth", "msg": ""}',     # bad msg
    '{"ts": 1790600000, "level": "INFO", "service": "auth", "msg": "ok", "attrs": [1,2]}',  # bad attrs
    "[1, 2, 3]",
    '"just a string"',
    '{"ts": true, "level": "INFO", "service": "auth", "msg": "x"}',          # bool ts
    '{"level": "INFO", "service": "auth", "msg": "missing ts entirely"}',
]


def generate_events(seed: int = SEED, now: int = NOW, hours: int = HOURS):
    """Return the list of log-event dicts (no malformed lines)."""
    rng = random.Random(seed)
    events = []
    start = now - hours * 3600
    total_min = hours * 60
    for minute in range(total_min):
        m_start = start + minute * 60
        in_incident = INCIDENT_START <= m_start < INCIDENT_END
        for service, rates in BASE_RATES.items():
            for level, mean in rates.items():
                n = _poisson(rng, mean)
                for _ in range(n):
                    tmpl = rng.choice(TEMPLATES[(service, level)])
                    msg, attrs = tmpl(rng)
                    ts = m_start + rng.randint(0, 59)
                    events.append({
                        "ts": datetime.fromtimestamp(ts, tz=timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "level": level, "service": service,
                        "msg": msg, "attrs": attrs,
                    })
        if in_incident:
            for _ in range(_poisson(rng, INCIDENT_ERR_PER_MIN)):
                ts = m_start + rng.randint(0, 59)
                events.append({
                    "ts": datetime.fromtimestamp(ts, tz=timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "level": "ERROR", "service": "payments",
                    "msg": INCIDENT_SIG,
                    "attrs": {"gateway": "helios-pay", "timeout_ms": 30000},
                })
            for _ in range(_poisson(rng, INCIDENT_WARN_PER_MIN)):
                ts = m_start + rng.randint(0, 59)
                oid = _oid(rng)
                events.append({
                    "ts": datetime.fromtimestamp(ts, tz=timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "level": "WARN", "service": "payments",
                    "msg": INCIDENT_WARN_TMPL.format(oid=oid),
                    "attrs": {"order_id": oid},
                })
    events.sort(key=lambda e: e["ts"])
    return events


def build_lines(seed: int = SEED, now: int = NOW, hours: int = HOURS):
    """Full JSONL line list including deterministically-placed malformed lines."""
    events = generate_events(seed=seed, now=now, hours=hours)
    lines = [json.dumps(e) for e in events]
    rng = random.Random(seed + 1)
    positions = sorted(rng.sample(range(len(lines)), min(len(MALFORMED), len(lines))))
    for i, pos in enumerate(positions):
        lines.insert(pos + i, MALFORMED[i % len(MALFORMED)])
    return lines


def main(out: str = OUT, seed: int = SEED, now: int = NOW, hours: int = HOURS):
    lines = build_lines(seed=seed, now=now, hours=hours)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} lines -> {out}")


if __name__ == "__main__":
    main()
