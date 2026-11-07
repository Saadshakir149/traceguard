"""Shared constants and small data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

ORDER_CREATED = "ORDER_CREATED"
PAYMENT_FAILED = "PAYMENT_FAILED"
PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
REFUND_ISSUED = "REFUND_ISSUED"
CHARGEBACK_OPENED = "CHARGEBACK_OPENED"
NOTE = "NOTE"

SUPPORTED_TYPES = frozenset(
    {ORDER_CREATED, PAYMENT_FAILED, PAYMENT_CAPTURED, REFUND_ISSUED, CHARGEBACK_OPENED, NOTE}
)
PAYMENT_TYPES = frozenset({PAYMENT_FAILED, PAYMENT_CAPTURED, REFUND_ISSUED, CHARGEBACK_OPENED})
# Records whose corruption makes the money picture untrustworthy.
MONEY_MOVING_TYPES = frozenset({ORDER_CREATED, PAYMENT_CAPTURED, REFUND_ISSUED, CHARGEBACK_OPENED})

CHECKOUT_SOURCE = "checkout"
PAYMENTS_SOURCE = "payments"

STATUSES = (
    "PAID",
    "UNPAID",
    "PARTIALLY_PAID",
    "PARTIALLY_REFUNDED",
    "REFUNDED",
    "DISPUTED",
    "UNKNOWN",
)


class Severity(str, Enum):
    ERROR = "error"      # integrity failure -> status UNKNOWN
    WARNING = "warning"  # status still decidable, confidence drops to MEDIUM
    INFO = "info"        # handled normally (e.g. duplicate ignored), no confidence impact


@dataclass(frozen=True)
class Anomaly:
    code: str
    severity: Severity
    message: str
    event_ids: tuple[str, ...] = ()
    lines: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "event_ids": list(self.event_ids),
        }
        if self.lines:
            out["lines"] = list(self.lines)
        return out


@dataclass(frozen=True)
class Record:
    """One parsed JSONL object plus the line it came from."""

    data: dict[str, Any]
    line: int | None = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def event_id(self) -> Any:
        return self.data.get("event_id")

    @property
    def type(self) -> Any:
        return self.data.get("type")


@dataclass(frozen=True)
class BadLine:
    line: int
    reason: str
    raw: str


@dataclass
class LoadResult:
    records: list[Record] = field(default_factory=list)
    bad_lines: list[BadLine] = field(default_factory=list)
