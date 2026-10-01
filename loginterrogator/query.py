"""Heuristic, fully-offline natural-language -> SQL over the log schema.

No API key, no network. A small pattern grammar maps questions to
parameterized SQLite queries; anything that does not parse as a log question
is *refused* (never answered by invention).

An optional OpenAI-compatible LLM backend sits behind the same interface:
set LOGS_LLM_API_URL / LOGS_LLM_API_KEY / LOGS_LLM_MODEL and pass
use_llm=True to QueryEngine. It is never exercised by tests or evals.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .ingest import db_max_ts

SERVICES = ["api-gateway", "orders", "inventory", "auth", "payments"]

LEVEL_WORDS = {
    "error": ("ERROR", "CRITICAL"),
    "errors": ("ERROR", "CRITICAL"),
    "err": ("ERROR", "CRITICAL"),
    "warning": ("WARN",),
    "warnings": ("WARN",),
    "warn": ("WARN",),
    "critical": ("CRITICAL",),
    "crit": ("CRITICAL",),
    "info": ("INFO",),
    "debug": ("DEBUG",),
}

DESTRUCTIVE = re.compile(
    r"\b(delete|drop|truncate|shutdown|kill|rm\s+-rf|destroy|wipe)\b", re.IGNORECASE)
INJECTION = re.compile(
    r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions\b", re.IGNORECASE)
NON_LOG_TOPICS = re.compile(
    r"\b(weather|poem|joke|recipe|horoscope|stock price|bitcoin|who are you|"
    r"write me|users? signed up|revenue|how old are you)\b", re.IGNORECASE)

_TS_RE = r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)"


@dataclass
class Answer:
    question: str
    intent: str
    refused: bool = False
    refusal_reason: str = ""
    sql: str = ""
    params: tuple = ()
    window_label: str = ""
    window: tuple[int, int] = (0, 0)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    summary: str = ""


def _parse_dt(s: str) -> int:
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"unparseable datetime: {s!r}")


def parse_window(text: str, now: int) -> tuple[int, int, str]:
    """Parse a time window from free text, relative to `now` (epoch secs)."""
    t = text.lower()
    m = re.search(r"last\s+(\d+)\s+(minute|hour|day|week)s?\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        secs = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}[unit]
        start = now - n * secs
        return start, now, f"last {n} {unit}{'s' if n != 1 else ''}"
    if re.search(r"\byesterday\b", t):
        day = datetime.fromtimestamp(now, tz=timezone.utc).date() - timedelta(days=1)
        s = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        return s, s + 86400, "yesterday"
    if re.search(r"\btoday\b", t):
        day = datetime.fromtimestamp(now, tz=timezone.utc).date()
        s = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        return s, now, "today"
    m = re.search(r"\bbetween\s+" + _TS_RE + r"\s+and\s+" + _TS_RE, t)
    if m:
        s, e = _parse_dt(m.group(1)), _parse_dt(m.group(2))
        if e <= s:
            raise ValueError("end of window is not after start")
        return s, e, f"between {m.group(1)} and {m.group(2)}"
    m = re.search(r"\bsince\s+" + _TS_RE, t)
    if m:
        s = _parse_dt(m.group(1))
        return s, now, f"since {m.group(1)}"
    return now - 86400, now, "last 24 hours"


def _detect_services(text: str) -> list[str]:
    t = text.lower().replace("api gateway", "api-gateway")
    return [s for s in SERVICES if s in t]


def _detect_levels(text: str) -> tuple[str, ...] | None:
    t = text.lower()
    if re.search(r"\berror rate\b", t):
        return ("ERROR", "CRITICAL")
    found: list[str] = []
    for word, lvls in LEVEL_WORDS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", t):
            for lv in lvls:
                if lv not in found:
                    found.append(lv)
    if re.search(r"\blogs?\b", t) and not found:
        return None  # "logs" with no level word -> all levels
    return tuple(found) if found else None


class QueryEngine:
    def __init__(self, db_path: str, now: int | None = None, use_llm: bool = False):
        self.db_path = db_path
        self.now = now if now is not None else (db_max_ts(db_path) or 0)
        self.use_llm = use_llm

    # -- public API -----------------------------------------------------
    def ask(self, question: str) -> Answer:
        q = question.strip()
        refusal = self._refusal_screen(q)
        if refusal:
            return Answer(question=q, intent="refuse", refused=True,
                          refusal_reason=refusal)
        if self.use_llm:
            llm_sql = maybe_llm_sql(q)
            if llm_sql:
                return self._run_llm_sql(q, llm_sql)
        return self._heuristic(q)

    # -- refusal ---------------------------------------------------------
    def _refusal_screen(self, q: str) -> str | None:
        if DESTRUCTIVE.search(q):
            return ("I can't run destructive commands. I only read logs — "
                    "ask me about counts, errors, or anomalies.")
        if INJECTION.search(q):
            return ("That looks like an instruction override. I only answer "
                    "questions about the ingested logs.")
        if NON_LOG_TOPICS.search(q):
            return ("That's outside what I can answer — I only know what's in "
                    "the ingested application logs.")
        return None

    # -- heuristic path --------------------------------------------------
    def _heuristic(self, q: str) -> Answer:
        t = q.lower()
        try:
            start, end, label = parse_window(t, self.now)
        except ValueError as e:
            return Answer(question=q, intent="refuse", refused=True,
                          refusal_reason=f"I couldn't parse the time window: {e}")
        services = _detect_services(t)
        levels = _detect_levels(t)

        if re.search(r"\bnew\b", t) and re.search(r"\b(signatures?|errors?)\b", t):
            return self._q_new_signatures(q, start, end, label, services, levels)
        if re.search(r"\btop\b", t) and re.search(r"\b(signatures?|errors?)\b", t):
            return self._q_top_signatures(q, start, end, label, services, levels)
        if re.search(r"\bmost common\b", t):
            return self._q_top_signatures(q, start, end, label, services, levels)
        if re.search(r"\berror rate\b", t):
            return self._q_error_rate(q, start, end, label, services)
        if re.search(r"\b(by|per) service\b", t):
            return self._q_group_service(q, start, end, label, services, levels)
        if re.search(r"\bhow many\b|\bcount\b|\bcounts\b", t):
            return self._q_count(q, start, end, label, services, levels)
        if re.search(r"\b(show|list|latest|recent)\b", t):
            return self._q_samples(q, start, end, label, services, levels)
        if re.search(r"\bbetween\b|\bwhat happened\b|\bsummary\b|\bsummarize\b", t):
            return self._q_summary(q, start, end, label, services)
        return Answer(question=q, intent="refuse", refused=True,
                      refusal_reason=("I couldn't map that to a log question. Try: "
                                      "'how many errors in the last 2 hours', "
                                      "'top error signatures by service yesterday', "
                                      "'error rate for payments today'."))

    # -- SQL builders ----------------------------------------------------
    def _where(self, start, end, services, levels):
        clauses = ["ts >= ?", "ts < ?"]
        params: list = [start, end]
        if services:
            clauses.append("service IN (%s)" % ",".join("?" * len(services)))
            params += services
        if levels:
            clauses.append("level IN (%s)" % ",".join("?" * len(levels)))
            params += list(levels)
        return " AND ".join(clauses), params

    def _run(self, ans: Answer) -> Answer:
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute(ans.sql, ans.params)
            ans.columns = [d[0] for d in cur.description] if cur.description else []
            ans.rows = cur.fetchall()
        finally:
            con.close()
        return ans

    def _q_count(self, q, start, end, label, services, levels):
        where, params = self._where(start, end, services, levels)
        lvl = "/".join(levels) if levels else "logs"
        svc = ",".join(services) if services else "all services"
        ans = Answer(question=q, intent="count", window_label=label,
                     window=(start, end),
                     sql=f"SELECT COUNT(*) FROM logs WHERE {where}",
                     params=tuple(params))
        self._run(ans)
        n = ans.rows[0][0]
        ans.summary = (f"{n:,} {lvl} entries for {svc} {label}.")
        return ans

    def _q_group_service(self, q, start, end, label, services, levels):
        where, params = self._where(start, end, services, levels)
        lvl = "/".join(levels) if levels else "logs"
        ans = Answer(question=q, intent="count_by_service", window_label=label,
                     window=(start, end),
                     sql=(f"SELECT service, COUNT(*) AS n FROM logs WHERE {where} "
                          "GROUP BY service ORDER BY n DESC, service ASC"),
                     params=tuple(params))
        self._run(ans)
        parts = ", ".join(f"{s}: {n:,}" for s, n in ans.rows)
        ans.summary = f"{lvl} by service {label} — " + (parts or "none.")
        return ans

    def _q_error_rate(self, q, start, end, label, services):
        where_all, p_all = self._where(start, end, services, None)
        where_err, p_err = self._where(start, end, services, ("ERROR", "CRITICAL"))
        svc = ",".join(services) if services else "all services"
        if services:
            sql = (f"SELECT service, "
                   f"(SELECT COUNT(*) FROM logs WHERE {where_err} AND service = l.service) AS err, "
                   f"COUNT(*) AS total FROM logs l WHERE {where_all} "
                   "GROUP BY service ORDER BY service ASC")
            # rebuild params: err subquery params + outer params
            _, pe = self._where(start, end, services, ("ERROR", "CRITICAL"))
            _, pa = self._where(start, end, services, None)
            params: tuple = tuple(pe + pa)
        else:
            sql = (f"SELECT 'all' AS service, "
                   f"(SELECT COUNT(*) FROM logs WHERE {where_err}) AS err, "
                   f"COUNT(*) AS total FROM logs WHERE {where_all}")
            params = tuple(p_err + p_all)
        ans = Answer(question=q, intent="error_rate", window_label=label,
                     window=(start, end), sql=sql, params=params)
        self._run(ans)
        out_rows = []
        parts = []
        for svc_name, err, total in ans.rows:
            rate = (err / total) if total else 0.0
            out_rows.append((svc_name, err, total, round(rate, 4)))
            parts.append(f"{svc_name}: {rate:.2%} ({err:,}/{total:,})")
        ans.columns = ["service", "errors", "total", "error_rate"]
        ans.rows = out_rows
        ans.summary = f"Error rate {label} — " + ("; ".join(parts) or "no logs.")
        return ans

    def _q_top_signatures(self, q, start, end, label, services, levels):
        levels = levels or ("ERROR", "CRITICAL")
        where, params = self._where(start, end, services, levels)
        ans = Answer(question=q, intent="top_signatures", window_label=label,
                     window=(start, end),
                     sql=(f"SELECT template, COUNT(*) AS n FROM logs WHERE {where} "
                          "GROUP BY sig, template ORDER BY n DESC, template ASC LIMIT 10"),
                     params=tuple(params))
        self._run(ans)
        svc = ",".join(services) if services else "all services"
        if ans.rows:
            top, n = ans.rows[0]
            ans.summary = (f"Top {'/'.join(levels)} signature for {svc} {label}: "
                           f"\"{top}\" ({n:,} occurrences).")
        else:
            ans.summary = f"No {'/'.join(levels)} entries for {svc} {label}."
        return ans

    def _q_new_signatures(self, q, start, end, label, services, levels):
        levels = levels or ("ERROR", "CRITICAL")
        where, params = self._where(start, end, services, levels)
        # "new" means first seen anywhere in history at/after window start --
        # compare against the whole table, not just the window's rows.
        ans = Answer(question=q, intent="new_signatures", window_label=label,
                     window=(start, end),
                     sql=(f"SELECT template, COUNT(*) AS n, MIN(ts) AS first_seen "
                          f"FROM logs WHERE {where} GROUP BY sig, template "
                          f"HAVING (SELECT MIN(ts) FROM logs l2 WHERE l2.sig = logs.sig) >= ? "
                          f"ORDER BY n DESC, template ASC LIMIT 10"),
                     params=tuple(params + [start]))
        self._run(ans)
        svc = ",".join(services) if services else "all services"
        if ans.rows:
            names = ", ".join(f"\"{t}\" ({n:,})" for t, n, _ in ans.rows)
            ans.summary = (f"New {'/'.join(levels)} signatures for {svc} {label}: {names}.")
        else:
            ans.summary = f"No new {'/'.join(levels)} signatures for {svc} {label}."
        return ans

    def _q_samples(self, q, start, end, label, services, levels):
        where, params = self._where(start, end, services, levels)
        ans = Answer(question=q, intent="samples", window_label=label,
                     window=(start, end),
                     sql=(f"SELECT ts, level, service, msg FROM logs WHERE {where} "
                          "ORDER BY ts DESC LIMIT 5"),
                     params=tuple(params))
        self._run(ans)
        ans.summary = f"Latest {len(ans.rows)} matching entries {label}."
        return ans

    def _q_summary(self, q, start, end, label, services):
        where, params = self._where(start, end, services, None)
        ans = Answer(question=q, intent="summary", window_label=label,
                     window=(start, end),
                     sql=(f"SELECT level, service, COUNT(*) AS n FROM logs WHERE {where} "
                          "GROUP BY level, service ORDER BY n DESC LIMIT 20"),
                     params=tuple(params))
        self._run(ans)
        total = sum(n for _, _, n in ans.rows)
        errs = sum(n for lv, _, n in ans.rows if lv in ("ERROR", "CRITICAL"))
        ans.summary = (f"{label}: {total:,} log entries, {errs:,} errors/criticals "
                       f"({errs / total:.1%} of volume)." if total else
                       f"{label}: no log entries.")
        return ans

    def _run_llm_sql(self, q: str, sql: str) -> Answer:
        # The LLM backend returns a single read-only SELECT; still executed
        # through the guardrail: only SELECT/WITH, single statement.
        s = sql.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", s) or ";" in s:
            return Answer(question=q, intent="refuse", refused=True,
                          refusal_reason="The LLM backend returned a non-read-only query; refusing to run it.")
        ans = Answer(question=q, intent="llm_sql", sql=s, params=())
        return self._run(ans)


class LLMNotConfigured(Exception):
    pass


def maybe_llm_sql(question: str) -> str | None:
    """Ask the optional OpenAI-compatible backend to translate NL -> SQL.

    Returns the SQL string, or None when the LOGS_LLM_* env vars are unset
    (the normal, fully-offline path). Raises on HTTP/API errors so a
    half-configured backend fails loudly instead of silently degrading.
    """
    url = os.environ.get("LOGS_LLM_API_URL")
    key = os.environ.get("LOGS_LLM_API_KEY")
    model = os.environ.get("LOGS_LLM_MODEL", "gpt-4o-mini")
    if not url or not key:
        return None
    schema_hint = ("Table logs(ts INTEGER unix, level TEXT, service TEXT, "
                   "msg TEXT, attrs TEXT json, sig TEXT, template TEXT). "
                   "Levels: DEBUG INFO WARN ERROR CRITICAL. "
                   "Services: api-gateway orders inventory auth payments.")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system",
             "content": ("Translate the user question into ONE read-only SQLite "
                         "SELECT query over the log table. Reply with ONLY the SQL, "
                         "no markdown. " + schema_hint)},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def ask(question: str, db_path: str, now: int | None = None) -> Answer:
    """One-shot convenience wrapper."""
    return QueryEngine(db_path, now=now).ask(question)
