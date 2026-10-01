# log-interrogator

**The on-call assistant every fintech FDE team wishes it had: ask plain-English questions about application logs and get SQL-backed answers, plus automatic anomaly flagging when something starts burning.**

Forward deployed engineers live inside customer incidents — and incidents live inside logs. The difference between a 10-minute triage and a 2-hour one is how fast you can answer "how many errors did payments log yesterday?", "what's the top error signature?", and "is this new or has it happened before?". This project is a complete, dependency-free implementation of that loop: JSONL logs go into SQLite, a heuristic NL→SQL engine (no API key, no network) answers questions over them, and an anomaly scanner flags error spikes, brand-new error signatures, and per-service level surges — each finding carrying its evidence. A golden eval suite proves the answers, the incident detection, the refusals, and the determinism.

> **Sample data only.** All 14,661 log lines are fictional, machine-generated
> (seeded RNG, fixed clock) for the made-up "Helios Home" storefront. No API
> keys, no network, no personal data — everything runs offline.

## Quickstart

```bash
python3 generate_sample.py                    # regenerate sample/logs.jsonl (byte-identical)
python3 cli.py ingest sample/logs.jsonl logs.db
python3 cli.py ask "how many errors did payments log yesterday" --db logs.db
python3 cli.py ask "what are the top error signatures in the last 24 hours" --db logs.db
python3 cli.py anomalies --db logs.db --scan-start 2026-09-28T06:00 --scan-end 2026-09-28T12:00
python3 cli.py serve --db logs.db             # tiny JSON API on 127.0.0.1:8472 (/ask, /anomalies)
python3 demo.py                               # one-command end-to-end: ingest -> 3 questions -> anomaly scan
python3 -m unittest discover -s tests -t .    # 63 hermetic tests
python3 evals/run_evals.py                    # 13 golden evals -> evals/eval_report.json
```

Ask from Python:

```python
from loginterrogator import ask, scan_anomalies

ans = ask("what is the error rate for payments today", "logs.db")
# ans.intent / ans.sql / ans.rows / ans.summary
# ans.refused + ans.refusal_reason when the question isn't answerable from logs

findings = scan_anomalies("logs.db", scan_start, scan_end, baseline_hours=24)
for f in findings:
    print(f.headline())   # "[payments] ERROR/CRITICAL spike: 2,116 in window vs ~4.0 expected (529.0x)"
```

## Example session

```
$ python3 cli.py ask "how many errors did payments log yesterday" --db logs.db
window: yesterday
2,122 ERROR/CRITICAL entries for payments yesterday.

$ python3 cli.py ask "any new error signatures in the last 48 hours" --db logs.db
New ERROR/CRITICAL signatures for all services last 48 hours:
  "paymentprocessor timeout after <n>ms (gateway=helios-pay)" (2,112)

$ python3 cli.py anomalies --db logs.db --scan-start 2026-09-28T06:00 --scan-end 2026-09-28T12:00
- [payments] ERROR/CRITICAL spike: 2,116 in window vs ~4.0 expected from baseline (529.0x)
- [payments] 1 new ERROR/CRITICAL signature(s): "paymentprocessor timeout after <n>ms (gateway=helios-pay)" (2,112)
- [payments] WARN spike: 562 in window vs ~17.8 expected from baseline (31.7x)
```

## Architecture

```
sample/logs.jsonl (14,661 lines, SIMULATED)
        |
        v  +------------------+
        +->| ingest.py        |  JSONL -> SQLite table logs
           |                  |  (ts, level, service, msg, attrs JSON,
           |  - validates     |   sig, template)
           |    every line    |  sig = sha256(normalized template)[:16]:
           |  - malformed /   |  numbers/UUIDs/IPs collapsed so
           |    invalid lines |  "order ORD-881231 failed" and
           |    counted,      |  "order ORD-881232 failed" share one signature
           |    skipped,      |
           |    reported      |
           +--------+---------+
                    |
        +----------+----------+
        |                     |
        v                     v
  +-----------+        +----------------+
  | query.py  |        | anomalies.py   |
  |           |        |                |
  | NL question ->    | scan window vs |
  |  1. refusal      | trailing       |
  |     screen       | baseline:      |
  |  2. time-window  |  - error spikes|
  |     parse        |    (>=4x base-  |
  |  3. intent ->    |    line rate,   |
  |     parameterized|    >=15 events) |
  |     SELECT       |  - new sigs    |
  |  intents: count, |    (first seen |
  |  by-service,     |    anywhere in |
  |  error-rate,     |    history is  |
  |  top-sigs,       |    in-window)  |
  |  new-sigs,       |  - WARN surges |
  |  samples,        |  every finding |
  |  summary         |  carries counts,|
  +----------------+  windows, sample|
  | optional:        |  messages       |
  | LOGS_LLM_* hook |                 |
  | (OpenAI-compat, |                 |
  |  never used in  |                 |
  |  tests/evals)   |                 |
  +----------------+  +----------------+
```

**Key design decisions**

- **Signatures, not strings.** Every message is normalized (numbers, UUIDs, IPs, hex tokens become `<n>`/`<uuid>`/`<ip>`/`<hex>`) and hashed. "Top error signatures" and "new error signatures" group by *kind of failure*, which is what on-call actually cares about.
- **"New" means new to history, not new to the window.** The new-signature queries compare first-seen against the *whole table* via a correlated subquery — a rare-but-old error that happens to recur is never misreported as new. (This was a real bug caught mid-build; see below.)
- **Refuse, don't invent.** Questions that don't parse as log questions — off-topic ("weather"), destructive ("delete all logs"), prompt-injection ("ignore previous instructions"), or about data that isn't in the logs ("users signed up") — get a refusal with a reason. No SQL is built, no answer is fabricated.
- **Deterministic everything.** Seeded sample generator with a fixed clock, injected `now` everywhere (the query engine defaults to `MAX(ts)` in the DB), sorted outputs, no wall clock, no randomness in the library. Evals must be byte-identical across runs.
- **Heuristic NL→SQL, honest about it.** The grammar covers the questions on-call actually asks (counts, by-service breakdowns, error rates, top/new signatures, samples, summaries, "between" windows). Anything outside the grammar is refused rather than guessed. An optional `LOGS_LLM_*` env hook swaps in an OpenAI-compatible model behind the same interface — but tests and evals never touch it, so the offline path is the proven one.

## Eval results

`python3 evals/run_evals.py` — 13 golden evals, all deterministic (two full runs must be byte-identical; the report is committed at `evals/eval_report.json`):

| Eval | Result |
|---|---|
| count_exact_payments_yesterday — engine == reference SQL == 2,122 | ✅ PASS |
| top_signature_payments_yesterday — incident signature, 2,112 | ✅ PASS |
| warnings_inventory_between — engine == reference SQL == 16 | ✅ PASS |
| error_rate_payments_today — 0.0301 ± 0.001 | ✅ PASS |
| errors_by_service_yesterday — exact row list (payments 2,122 … auth 5) | ✅ PASS |
| new_signatures_surface_incident — incident signature surfaced | ✅ PASS |
| anomaly_flags_incident — spike (2,116, 529x) + new-signature finding | ✅ PASS |
| anomaly_quiet_window_clean — zero findings pre-incident | ✅ PASS |
| refuse_off_topic ("weather in dallas") | ✅ PASS |
| refuse_destructive ("delete all logs") | ✅ PASS |
| refuse_unknown_data ("users signed up") | ✅ PASS |
| refuse_injection ("ignore all previous instructions…") | ✅ PASS |
| determinism — two full runs byte-identical | ✅ PASS |

## Config / threshold guidance

`scan_anomalies(db, scan_start, scan_end, baseline_hours=24, min_count=15, spike_factor=4.0)`:

- `spike_factor` (default **4.0**): flag when the scan-window count is ≥4x the baseline-implied expectation. Lower it (~2.5) for noisy services where you want earlier signal; raise it (~8) for spiky-but-healthy services.
- `min_count` (default **15**): absolute floor — a 10x jump from 1 event to 10 is not an incident, it's Tuesday. Keeps tiny samples from paging anyone.
- `baseline_hours` (default **24**): trailing baseline window. Use 168 (a week) for services with strong day-of-week seasonality; the demo data is 72h, so 24h is the honest default here.
- New-signature floor: ≥5 occurrences in the scan window, to avoid flagging one-off freak lines.

## Dev loop: bugs the tests actually caught

1. **New-signature detection compared against the window, not history.** The first version used `HAVING MIN(ts) >= :window_start` over the window's rows — so any rare-but-old signature that recurred inside the window was reported as "new". The demo exposed it: "new signatures in the last 48h" listed baseline errors that had existed for days. Fix: a correlated subquery `(SELECT MIN(ts) FROM logs l2 WHERE l2.sig = logs.sig) >= :window_start` over the whole table, in both `query.py` and `anomalies.py`. `test_new_signatures_ignores_pre_window_history` and `test_previously_seen_signature_not_new` lock the fix.
2. **Test fixture anchored at the wrong absolute time.** `baseline_db()` generated 30h of data starting at a fixed epoch while the scan window sat 72h later — the trailing baseline was empty, `expected=0`, and the detector (correctly) reported nothing. Two tests failed; the detector was right and the fixture was wrong. Fix: anchor the fixture so its final hours end exactly at the scan window.
3. **The min-count test tripped the new-signature gate.** The first draft injected 5 lines of a *novel* message to test the `min_count=15` spike floor — and the suite correctly flagged it as a new signature (≥5 occurrences), failing the "expect zero findings" assertion. That taught the real design point: the spike floor and the new-signature floor are independent gates. Fix: the test reuses a pre-existing baseline message, so it exercises only the `min_count` gate.
4. **A walrus-operator typo in the refusal fallthrough.** `refused_reason := ""` snuck into a keyword argument while writing the "I couldn't map that" path — a syntax error caught while re-reading the file before the first test run. Unremarkable except that it's exactly the path users hit most (unparseable questions), so it would have been the first thing to break in a demo.

## Known limitations

- The NL grammar is heuristic and English-only: it covers the on-call question shapes above and refuses the rest. It won't parse "compare Tuesday's error rate to Monday's" — that's what the `LOGS_LLM_*` hook is for.
- "Today"/"yesterday" are UTC day boundaries. Good enough for demo data; a production version would take a timezone.
- The anomaly detector is a rate-ratio heuristic, not a statistical model — no seasonality handling, no per-signature baselines. The thresholds are exposed so you can tune them against your own data.
- `serve` is a minimal stdlib HTTP server for demos, not production infrastructure.

## Layout

```
loginterrogator/     the library: ingest.py, query.py, anomalies.py
generate_sample.py   seeded, fixed-clock sample-log generator (byte-identical reruns)
sample/logs.jsonl    14,661 lines of SIMULATED Helios Home logs (72h + scripted incident)
cli.py               ingest | ask | anomalies | serve
demo.py              one-command end-to-end (ingest -> 3 questions -> incident scan -> quiet scan)
evals/run_evals.py   13 golden evals -> evals/eval_report.json (committed)
tests/               63 hermetic unittest tests (injected clocks, fixed seeds, temp DBs)
```

## License

MIT — see [LICENSE](LICENSE).
