"""log-interrogator: natural-language questions over application logs.

Ask plain-English questions about JSONL application logs (ingested into SQLite)
and get SQL-backed answers plus anomaly findings. Fully offline, stdlib-only,
deterministic.
"""

from .ingest import ingest_jsonl, signature_of
from .query import ask, QueryEngine, Answer
from .anomalies import scan_anomalies, Finding

__all__ = [
    "ingest_jsonl",
    "signature_of",
    "ask",
    "QueryEngine",
    "Answer",
    "scan_anomalies",
    "Finding",
]
