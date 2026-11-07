"""Deterministic reconciliation engine.

Input: the raw records for ONE (tenant, order). Output: a Decision.
No language model, no I/O, no dependence on input order.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .models import (
    CHARGEBACK_OPENED,
    CHECKOUT_SOURCE,
    MONEY_MOVING_TYPES,
    NOTE,
    ORDER_CREATED,
    PAYMENT_CAPTURED,
    PAYMENT_FAILED,
    PAYMENT_TYPES,
    PAYMENTS_SOURCE,
    REFUND_ISSUED,
    SUPPORTED_TYPES,
    Anomaly,
    Record,
    Severity,
)

E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# Fields that do not change an event's identity/meaning (rule 04).
NON_SUBSTANTIVE_FIELDS = frozenset({"ingested_at"})


# ---------------------------------------------------------------------------
# Typed, validated facts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderDef:
    event_id: str
    amount: int
    currency: str
    occurred_at: datetime


@dataclass(frozen=True)
class Failure:
    event_id: str
    attempt_id: str | None
    occurred_at: datetime
    reason: str | None


@dataclass(frozen=True)
class Capture:
    event_id: str
    capture_id: str
    attempt_id: str | None
    amount: int
    currency: str
    occurred_at: datetime


@dataclass(frozen=True)
class Refund:
    event_id: str
    refund_id: str
    capture_id: str
    amount: int
    currency: str
    occurred_at: datetime


@dataclass(frozen=True)
class Chargeback:
    event_id: str
    chargeback_key: str
    capture_id: str
    amount: int | None
    currency: str | None
    occurred_at: datetime


@dataclass
class Facts:
    """Everything the explanation layer is allowed to talk about."""

    tenant_id: str
    order_id: str
    order: OrderDef | None = None
    failures: list[Failure] = field(default_factory=list)
    captures: list[Capture] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    chargebacks: list[Chargeback] = field(default_factory=list)


@dataclass
class Decision:
    status: str
    captured_cents: int | None
    refunded_cents: int | None
    net_paid_cents: int | None
    anomalies: list[Anomaly]
    evidence_event_ids: list[str]
    facts: Facts


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
def parse_timestamp(value: Any) -> datetime | None:
    """ISO 8601 with an explicit offset. Naive timestamps are rejected
    because their ordering against other events is ambiguous."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if ts.tzinfo is None or ts.utcoffset() is None:
        return None
    return ts


def _nonempty_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_int(value: Any) -> int | None:
    # bool is a subclass of int; floats and numeric strings are rejected.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _currency(value: Any) -> str | None:
    return value if isinstance(value, str) and _CURRENCY_RE.match(value) else None


def _canonical(data: dict[str, Any]) -> str:
    return json.dumps(
        {k: v for k, v in data.items() if k not in NON_SUBSTANTIVE_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sort_key(rec: Record) -> tuple:
    ts = parse_timestamp(rec.get("occurred_at"))
    return (
        ts.timestamp() if ts else float("inf"),
        str(rec.event_id),
        str(rec.get("ingested_at")),
        _canonical(rec.data),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def reconcile(
    tenant_id: str,
    order_id: str,
    records: Iterable[Record],
    event_ids_reused_by_other_orders: dict[str, list[str]] | None = None,
) -> Decision:
    anomalies: list[Anomaly] = []
    facts = Facts(tenant_id, order_id)
    reused = event_ids_reused_by_other_orders or {}

    # Sort first so every later step sees the same sequence regardless of
    # the order records arrived in (rule 08).
    records = sorted(records, key=_sort_key)

    # 1. Scope: drop notes (never financial evidence) and foreign records.
    scoped: list[Record] = []
    for rec in records:
        if rec.type == NOTE:
            continue
        if rec.get("tenant_id") != tenant_id or rec.get("order_id") != order_id:
            anomalies.append(Anomaly("FOREIGN_RECORD_DROPPED", W,
                "A record for a different tenant/order was supplied and ignored.",
                lines=_lines([rec])))
            continue
        scoped.append(rec)

    # 2. Event identity (rules 04, 05).
    unique = _dedupe_events(scoped, reused, anomalies)

    # 3. Per-record validation into typed facts.
    orders: list[OrderDef] = []
    for rec in unique:
        _validate_record(rec, orders, facts, anomalies)

    # 4. Order definition (rule 01).
    _resolve_order(orders, facts, anomalies)

    # 5. Currency consistency.
    if facts.order is not None:
        _check_currencies(facts, anomalies)

    # 6. Transaction identity (rule 06).
    facts.captures = _dedupe_transactions(
        facts.captures, "capture_id", ("attempt_id", "amount", "currency"), "capture", anomalies)
    facts.refunds = _dedupe_transactions(
        facts.refunds, "refund_id", ("capture_id", "amount", "currency"), "refund", anomalies)
    facts.chargebacks = _dedupe_transactions(
        facts.chargebacks, "chargeback_key", ("capture_id", "amount", "currency"), "chargeback", anomalies)

    # 7. References and limits (rules 07, 10).
    _check_refunds(facts, anomalies)
    _check_chargebacks(facts, anomalies)
    captured = sum(c.amount for c in facts.captures)
    refunded = sum(r.amount for r in facts.refunds)
    if facts.order is not None and captured > facts.order.amount:
        anomalies.append(Anomaly("OVER_CAPTURE", E,
            f"Captured {captured} cents exceeds the order amount of {facts.order.amount} cents.",
            tuple(c.event_id for c in facts.captures) + (facts.order.event_id,)))
    _check_attempts_and_timeline(facts, anomalies)

    # 8. Status by the required precedence.
    status = _decide_status(facts, captured, refunded, anomalies)
    if status == "UNKNOWN":
        totals: tuple[int | None, int | None, int | None] = (None, None, None)
    else:
        totals = (captured, refunded, captured - refunded)

    anomalies = _stable_unique(anomalies)
    evidence = _evidence(status, facts, anomalies, records)
    return Decision(status, *totals, anomalies, evidence, facts)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def _lines(recs: Iterable[Record]) -> tuple[int, ...]:
    return tuple(sorted({r.line for r in recs if r.line is not None}))


def _type_in(rec: Record, types: frozenset[str]) -> bool:
    # `type` comes from untrusted JSON and may be a list/dict (unhashable).
    return isinstance(rec.type, str) and rec.type in types


_FINANCIAL_OR_ORDER = PAYMENT_TYPES | {ORDER_CREATED}


def _dedupe_events(scoped: list[Record], reused: dict[str, list[str]], anomalies: list[Anomaly]) -> list[Record]:
    groups: dict[str, list[Record]] = defaultdict(list)
    unique: list[Record] = []
    for rec in scoped:
        eid = _nonempty_str(rec.event_id)
        if eid is None:
            sev = E if _type_in(rec, MONEY_MOVING_TYPES) else W
            label = rec.type if isinstance(rec.type, str) and rec.type else "untyped"
            anomalies.append(Anomaly("MISSING_EVENT_ID", sev,
                f"A {label} record has no event_id and cannot be verified.",
                lines=_lines([rec])))
            continue
        groups[eid].append(rec)

    for eid in sorted(groups):
        group = groups[eid]
        if eid in reused:
            sev = E if any(_type_in(r, _FINANCIAL_OR_ORDER) for r in group) else W
            anomalies.append(Anomaly("EVENT_ID_REUSED_ACROSS_ORDERS", sev,
                f"Event id {eid} is also used by order(s) {', '.join(reused[eid])} in this tenant; "
                "its identity is ambiguous so it was not used.", (eid,), _lines(group)))
            continue
        versions = {_canonical(r.data) for r in group}
        if len(versions) > 1:
            sev = E if any(_type_in(r, _FINANCIAL_OR_ORDER) for r in group) else W
            anomalies.append(Anomaly("CONFLICTING_EVENT_IDENTITY", sev,
                f"{len(group)} records share event id {eid} but disagree on substantive fields; "
                "none of them was used.", (eid,), _lines(group)))
            continue
        unique.append(group[0])  # group already sorted; identical content
        if len(group) > 1:
            anomalies.append(Anomaly("DUPLICATE_EVENT_IGNORED", I,
                f"Event {eid} was delivered {len(group)} times (only ingested_at differs); counted once.",
                (eid,), _lines(group)))
    return unique


def _validate_record(rec: Record, orders: list[OrderDef], facts: Facts, anomalies: list[Anomaly]) -> None:
    eid: str = rec.event_id
    etype = rec.type
    source = rec.get("source")

    if not isinstance(etype, str) or etype not in SUPPORTED_TYPES:
        anomalies.append(Anomaly("UNSUPPORTED_EVENT_TYPE", W,
            f"Event {eid} has unsupported type {etype!r}; ignored. The payment history may be incomplete.",
            (eid,), _lines([rec])))
        return

    authority = CHECKOUT_SOURCE if etype == ORDER_CREATED else PAYMENTS_SOURCE
    if source != authority:
        anomalies.append(Anomaly("NON_AUTHORITATIVE_SOURCE", I,
            f"Event {eid} ({etype}) came from source {source!r}; only {authority!r} may establish it, "
            "so it was not used as evidence.", (eid,), _lines([rec])))
        return

    problems: list[str] = []
    occurred = parse_timestamp(rec.get("occurred_at"))
    if occurred is None:
        problems.append("occurred_at is missing or not a valid ISO 8601 timestamp with offset")
    if "ingested_at" in rec.data and parse_timestamp(rec.get("ingested_at")) is None:
        problems.append("ingested_at is not a valid ISO 8601 timestamp with offset")

    amount = _positive_int(rec.get("amount_cents"))
    currency = _currency(rec.get("currency"))
    attempt = _nonempty_str(rec.get("attempt_id"))
    capture_id = _nonempty_str(rec.get("capture_id"))

    def need(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    if etype in (ORDER_CREATED, PAYMENT_CAPTURED, REFUND_ISSUED):
        need(amount is not None, "amount_cents must be a positive integer")
        need(currency is not None, "currency must be a 3-letter ISO code")
    if etype in (PAYMENT_CAPTURED, REFUND_ISSUED, CHARGEBACK_OPENED):
        need(capture_id is not None, "capture_id is required")
    if etype == REFUND_ISSUED:
        need(_nonempty_str(rec.get("refund_id")) is not None, "refund_id is required")
    if etype == CHARGEBACK_OPENED:
        if rec.get("amount_cents") is not None:
            need(amount is not None, "amount_cents, when present, must be a positive integer")
        if rec.get("currency") is not None:
            need(currency is not None, "currency, when present, must be a 3-letter ISO code")

    if problems:
        sev = E if etype in MONEY_MOVING_TYPES else W
        anomalies.append(Anomaly("INVALID_RECORD", sev,
            f"Event {eid} ({etype}) is malformed: {'; '.join(problems)}. It was not used.",
            (eid,), _lines([rec])))
        return

    assert occurred is not None
    if etype == ORDER_CREATED:
        orders.append(OrderDef(eid, amount, currency, occurred))  # type: ignore[arg-type]
    elif etype == PAYMENT_FAILED:
        reason = rec.get("reason") if isinstance(rec.get("reason"), str) else None
        facts.failures.append(Failure(eid, attempt, occurred, reason))
    elif etype == PAYMENT_CAPTURED:
        facts.captures.append(Capture(eid, capture_id, attempt, amount, currency, occurred))  # type: ignore[arg-type]
    elif etype == REFUND_ISSUED:
        facts.refunds.append(Refund(eid, rec.get("refund_id").strip(), capture_id, amount, currency, occurred))  # type: ignore[arg-type]
    elif etype == CHARGEBACK_OPENED:
        key = _nonempty_str(rec.get("chargeback_id")) or eid
        facts.chargebacks.append(Chargeback(eid, key, capture_id, amount, currency, occurred))  # type: ignore[arg-type]


def _resolve_order(orders: list[OrderDef], facts: Facts, anomalies: list[Anomaly]) -> None:
    if not orders:
        anomalies.append(Anomaly("MISSING_ORDER_DEFINITION", E,
            "No valid ORDER_CREATED record from checkout; the expected amount is unknown."))
        return
    definitions = {(o.amount, o.currency) for o in orders}
    if len(definitions) > 1:
        anomalies.append(Anomaly("CONFLICTING_ORDER_DEFINITION", E,
            "Checkout records disagree on the order amount/currency: "
            + "; ".join(f"{o.event_id}={o.amount} {o.currency}" for o in orders) + ".",
            tuple(o.event_id for o in orders)))
        return
    if len(orders) > 1:
        anomalies.append(Anomaly("REDUNDANT_ORDER_DEFINITION", I,
            "Several consistent ORDER_CREATED records found; treated as one order.",
            tuple(o.event_id for o in orders)))
    facts.order = orders[0]


def _check_currencies(facts: Facts, anomalies: list[Anomaly]) -> None:
    expected = facts.order.currency  # type: ignore[union-attr]

    def keep(items: list, kind: str) -> list:
        out = []
        for item in items:
            if item.currency is not None and item.currency != expected:
                anomalies.append(Anomaly("CURRENCY_MISMATCH", E,
                    f"{kind} {item.event_id} is in {item.currency} but the order is in {expected}.",
                    (item.event_id, facts.order.event_id)))  # type: ignore[union-attr]
            else:
                out.append(item)
        return out

    facts.captures = keep(facts.captures, "Capture")
    facts.refunds = keep(facts.refunds, "Refund")
    facts.chargebacks = keep(facts.chargebacks, "Chargeback")


def _dedupe_transactions(items: list, id_attr: str, fields: tuple[str, ...], kind: str,
                         anomalies: list[Anomaly]) -> list:
    groups: dict[str, list] = defaultdict(list)
    for item in items:
        groups[getattr(item, id_attr)].append(item)
    kept = []
    for txn_id in sorted(groups):
        group = sorted(groups[txn_id], key=lambda x: (x.occurred_at, x.event_id))
        ids = tuple(x.event_id for x in group)
        if len({tuple(getattr(x, f) for f in fields) for x in group}) > 1:
            anomalies.append(Anomaly("CONFLICTING_TRANSACTION_ID", E,
                f"Events {', '.join(ids)} reuse {kind} id {txn_id} with different details.", ids))
            continue
        kept.append(group[0])
        if len(group) > 1:
            anomalies.append(Anomaly("DUPLICATE_TRANSACTION_IGNORED", I,
                f"{kind.capitalize()} {txn_id} was reported by {len(group)} events ({', '.join(ids)}); "
                "counted once.", ids))
    return kept


def _check_refunds(facts: Facts, anomalies: list[Anomaly]) -> None:
    by_capture = {c.capture_id: c for c in facts.captures}
    valid: list[Refund] = []
    for r in facts.refunds:
        if r.capture_id not in by_capture:
            anomalies.append(Anomaly("INVALID_REFUND_REFERENCE", E,
                f"Refund {r.refund_id} ({r.event_id}) references capture {r.capture_id}, "
                "which is not a valid capture on this order.", (r.event_id,)))
        else:
            valid.append(r)
    for capture_id, capture in sorted(by_capture.items()):
        against = [r for r in valid if r.capture_id == capture_id]
        total = sum(r.amount for r in against)
        if total > capture.amount:
            anomalies.append(Anomaly("OVER_REFUND", E,
                f"Refunds against capture {capture_id} total {total} cents, more than the "
                f"{capture.amount} cents captured.", (capture.event_id,) + tuple(r.event_id for r in against)))
    # Keep only refunds with valid references so totals never include orphans.
    facts.refunds = valid


def _check_chargebacks(facts: Facts, anomalies: list[Anomaly]) -> None:
    capture_ids = {c.capture_id for c in facts.captures}
    valid = []
    for cb in facts.chargebacks:
        if cb.capture_id not in capture_ids:
            anomalies.append(Anomaly("INVALID_CHARGEBACK_REFERENCE", E,
                f"Chargeback {cb.event_id} references capture {cb.capture_id}, "
                "which is not a valid capture on this order.", (cb.event_id,)))
        else:
            valid.append(cb)
    facts.chargebacks = valid


def _check_attempts_and_timeline(facts: Facts, anomalies: list[Anomaly]) -> None:
    captured_attempts = {c.attempt_id: c for c in facts.captures if c.attempt_id}
    for f in facts.failures:
        if f.attempt_id in captured_attempts:
            c = captured_attempts[f.attempt_id]
            anomalies.append(Anomaly("ATTEMPT_CONFLICT", W,
                f"Attempt {f.attempt_id} is reported both failed ({f.event_id}) and captured "
                f"({c.event_id}); the capture is used as the stronger evidence.", (f.event_id, c.event_id)))
    by_capture = {c.capture_id: c for c in facts.captures}
    for item in [*facts.refunds, *facts.chargebacks]:
        c = by_capture.get(item.capture_id)
        if c is not None and item.occurred_at < c.occurred_at:
            anomalies.append(Anomaly("TIMELINE_INCONSISTENT", W,
                f"{item.event_id} occurred before the capture it references ({c.event_id}); "
                "possible clock skew or missing history.", (item.event_id, c.event_id)))


def _decide_status(facts: Facts, captured: int, refunded: int, anomalies: list[Anomaly]) -> str:
    if any(a.severity == E for a in anomalies):
        return "UNKNOWN"                                     # 1
    if facts.chargebacks:
        return "DISPUTED"                                    # 2
    if not facts.captures and facts.failures:
        return "UNPAID"                                      # 3
    if captured > 0 and refunded == captured:
        return "REFUNDED"                                    # 4
    if 0 < refunded < captured:
        return "PARTIALLY_REFUNDED"                          # 5
    if facts.order is not None and captured == facts.order.amount:
        return "PAID"                                        # 6
    if facts.order is not None and 0 < captured < facts.order.amount:
        return "PARTIALLY_PAID"                              # 7
    anomalies.append(Anomaly("INSUFFICIENT_PAYMENT_EVIDENCE", W,
        "No valid captures or verified payment failures exist; the payment history may be incomplete."))
    return "UNKNOWN"                                         # 8


def _stable_unique(anomalies: list[Anomaly]) -> list[Anomaly]:
    seen, out = set(), []
    order = {E: 0, W: 1, I: 2}
    for a in sorted(anomalies, key=lambda a: (order[a.severity], a.code, a.event_ids, a.lines, a.message)):
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _evidence(status: str, facts: Facts, anomalies: list[Anomaly], records: list[Record]) -> list[str]:
    if status == "UNKNOWN":
        ids = {eid for a in anomalies if a.severity != I for eid in a.event_ids}
        if facts.order is not None:
            ids.add(facts.order.event_id)
    else:
        ids = {facts.order.event_id} if facts.order else set()
        for group in (facts.failures, facts.captures, facts.refunds, facts.chargebacks):
            ids.update(x.event_id for x in group)
    # Only cite IDs that exist in the input, ordered by first occurrence time.
    position: dict[str, tuple] = {}
    for rec in records:
        eid = rec.event_id
        if isinstance(eid, str) and eid in ids and eid not in position:
            position[eid] = _sort_key(rec)[:2]
    return sorted(position, key=lambda e: position[e])
