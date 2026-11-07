"""Replaceable language-model layer.

The model may only (a) classify the question's intent and (b) re-word an
explanation that was already built from verified facts. Its output is
checked by `guard_wording`; anything that contradicts the computed decision
is discarded in favour of the deterministic text.
"""
from __future__ import annotations

import re
from typing import Protocol

from .models import STATUSES

INTENTS = ("PAYMENT_STATUS", "FAILURE_REASON", "EVIDENCE", "REFUND", "DISPUTE", "GENERAL")


class LanguageModel(Protocol):
    name: str

    def classify_intent(self, question: str) -> str: ...

    def reword(self, draft: str, status: str) -> str: ...


class RuleBasedModel:
    """Default offline 'model': keyword intent + identity rewording."""

    name = "rule-based"
    _RULES = [
        ("FAILURE_REASON", r"\bfail|declin|error|reject"),
        ("REFUND", r"refund"),
        ("DISPUTE", r"dispute|chargeback"),
        ("EVIDENCE", r"evidence|support|prove|proof|why|records?"),
        ("PAYMENT_STATUS", r"\bpaid\b|\bpay|status|captur|charged"),
    ]

    def classify_intent(self, question: str) -> str:
        q = (question or "").lower()
        for intent, pattern in self._RULES:
            if re.search(pattern, q):
                return intent
        return "GENERAL"

    def reword(self, draft: str, status: str) -> str:
        return draft


_STATUS_TOKEN = re.compile(r"\b(" + "|".join(sorted(STATUSES, key=len, reverse=True)) + r")\b")


def guard_intent(intent: str) -> str:
    return intent if intent in INTENTS else "GENERAL"


def guard_wording(candidate: object, draft: str, status: str, max_len: int = 2000) -> tuple[str, bool]:
    """Return (text, accepted). Reject model text that is not a string, is
    too long, omits the computed status, or names any other status."""
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > max_len:
        return draft, False
    mentioned = set(_STATUS_TOKEN.findall(candidate))
    if status not in mentioned or mentioned - {status}:
        return draft, False
    return candidate, True
