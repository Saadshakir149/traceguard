"""Support notes: untrusted free text. Read for context only, never as evidence.

Nothing in this module can influence status, totals, evidence or confidence;
every anomaly it produces is INFO severity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import NOTE, Anomaly, Record, Severity

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"system (override|prompt)",
    r"\bmark (this|the) order\b",
    r"do not (mention|reveal|tell)",
    r"you are (now )?(an?|the) ",
    r"\b(assistant|ai|model)\s*:",
    r"financial records are (outdated|wrong|incorrect)",
    r"set (the )?status",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True)
class NoteView:
    event_id: str
    source: object
    excerpt: str
    suspicious: bool


def analyze_notes(tenant_id: str, order_id: str, records: list[Record],
                  financial_event_ids: set[str]) -> tuple[list[NoteView], list[Anomaly]]:
    views: dict[str, NoteView] = {}
    anomalies: list[Anomaly] = []
    for rec in sorted(records, key=lambda r: (str(r.event_id), str(r.get("text")))):
        if rec.type != NOTE or rec.get("tenant_id") != tenant_id or rec.get("order_id") != order_id:
            continue
        eid = rec.event_id
        if not isinstance(eid, str) or not eid.strip():
            anomalies.append(Anomaly("NOTE_WITHOUT_ID", Severity.INFO,
                "A support note without event_id was ignored.",
                lines=(rec.line,) if rec.line else ()))
            continue
        if eid in financial_event_ids:
            anomalies.append(Anomaly("NOTE_EVENT_ID_COLLISION", Severity.INFO,
                f"Note reuses event id {eid} of a financial record; the note was ignored.", (eid,)))
            continue
        text = rec.get("text")
        if not isinstance(text, str):
            text = ""
        suspicious = bool(_INJECTION_RE.search(text))
        if eid in views:
            continue
        views[eid] = NoteView(eid, rec.get("source"), _excerpt(text), suspicious)
        if suspicious:
            anomalies.append(Anomaly("SUSPICIOUS_NOTE_CONTENT", Severity.INFO,
                f"Support note {eid} contains instruction-like text aimed at an AI system. "
                "It was treated as untrusted data and had no effect on the result.", (eid,)))
    return list(views.values()), anomalies


def _excerpt(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."
