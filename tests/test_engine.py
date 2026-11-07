"""Deterministic reconciliation rules."""
import pytest

from helpers import capture, chargeback, codes, decide, ev, failure, note, order, refund, ts


# --- valid cases -------------------------------------------------------------
def test_failed_attempt_then_capture_with_duplicate_is_paid():
    cap = capture("pay-2", "C2", 12900, attempt="A2")
    d = decide([order(), failure("pay-1", "A1"), cap, dict(cap, ingested_at=ts(500))])
    assert d.status == "PAID"
    assert (d.captured_cents, d.refunded_cents, d.net_paid_cents) == (12900, 0, 12900)
    assert d.evidence_event_ids == ["order-1", "pay-1", "pay-2"]
    assert codes(d) == ["DUPLICATE_EVENT_IGNORED"]


def test_partial_refund():
    d = decide([order(amount=8000), capture("p1", "C1", 8000), refund("r1", "R1", "C1", 2500)])
    assert d.status == "PARTIALLY_REFUNDED"
    assert (d.captured_cents, d.refunded_cents, d.net_paid_cents) == (8000, 2500, 5500)


def test_two_refunds_summing_to_capture_is_refunded():
    d = decide([order(amount=6000), capture("p1", "C1", 6000),
                refund("r1", "R1", "C1", 4000), refund("r2", "R2", "C1", 2000, at=60)])
    assert d.status == "REFUNDED"
    assert d.net_paid_cents == 0


def test_chargeback_is_disputed_and_does_not_change_totals():
    d = decide([order(amount=5000), capture("p1", "C1", 5000), refund("r1", "R1", "C1", 1000),
                chargeback("cb1", "C1")])
    assert d.status == "DISPUTED"
    assert (d.captured_cents, d.refunded_cents) == (5000, 1000)
    assert "cb1" in d.evidence_event_ids


def test_only_failures_is_unpaid():
    d = decide([order(), failure("f1", "A1"), failure("f2", "A2", at=3)])
    assert d.status == "UNPAID"
    assert (d.captured_cents, d.refunded_cents, d.net_paid_cents) == (0, 0, 0)


def test_partial_capture_is_partially_paid():
    d = decide([order(amount=10000), capture("p1", "C1", 4000)])
    assert d.status == "PARTIALLY_PAID"


# --- contradictory / invalid records -> UNKNOWN --------------------------------
@pytest.mark.parametrize("records, code", [
    ([order(amount=5000), capture("p1", "C1", 5000), capture("p2", "C2", 5000, attempt="A2")], "OVER_CAPTURE"),
    ([order("o-a", amount=7000), order("o-b", amount=7500), capture("p1", "C1", 7000)],
     "CONFLICTING_ORDER_DEFINITION"),
    ([order(), capture("p1", "C1", 12900), refund("r1", "R1", "C-MISSING", 100)], "INVALID_REFUND_REFERENCE"),
    ([order(amount=3000), capture("p1", "C1", 3000), refund("r1", "R1", "C1", 2000),
      refund("r2", "R2", "C1", 2000, at=60)], "OVER_REFUND"),
    ([order(), capture("p1", "C1", 12900, cur="EUR")], "CURRENCY_MISMATCH"),
    ([order(), capture("p1", "C1", 12900), chargeback("cb", "C-NOPE")], "INVALID_CHARGEBACK_REFERENCE"),
    ([capture("p1", "C1", 12900)], "MISSING_ORDER_DEFINITION"),
], ids=["over-capture", "conflicting-order", "bad-refund-ref", "over-refund", "currency",
        "bad-chargeback-ref", "missing-order"])
def test_integrity_failures_are_unknown_with_null_totals(records, code):
    d = decide(records)
    assert d.status == "UNKNOWN"
    assert d.captured_cents is d.refunded_cents is d.net_paid_cents is None
    assert code in codes(d)
    input_ids = {r["event_id"] for r in records}
    assert set(d.evidence_event_ids) <= input_ids


def test_same_event_id_with_different_amount_is_integrity_error():
    cap = capture("p1", "C1", 12900)
    d = decide([order(), cap, dict(cap, amount_cents=1290)])
    assert d.status == "UNKNOWN"
    assert "CONFLICTING_EVENT_IDENTITY" in codes(d)


def test_reused_capture_id_with_different_amount_is_error():
    d = decide([order(), capture("p1", "C1", 12900), capture("p2", "C1", 6000, attempt="A2")])
    assert d.status == "UNKNOWN"
    assert "CONFLICTING_TRANSACTION_ID" in codes(d)


def test_reused_refund_id_with_different_capture_is_error():
    d = decide([order(amount=2000), capture("p1", "C1", 1000), capture("p2", "C2", 1000, attempt="A2"),
                refund("r1", "R1", "C1", 500), refund("r2", "R1", "C2", 500, at=60)])
    assert d.status == "UNKNOWN"
    assert "CONFLICTING_TRANSACTION_ID" in codes(d)


def test_same_capture_reported_under_two_event_ids_counts_once():
    d = decide([order(), capture("p1", "C1", 12900), capture("p1-replay", "C1", 12900)])
    assert d.status == "PAID"
    assert d.captured_cents == 12900
    assert "DUPLICATE_TRANSACTION_IGNORED" in codes(d)


# --- malformed input -----------------------------------------------------------
@pytest.mark.parametrize("field, value", [
    ("amount_cents", "12900"), ("amount_cents", 129.0), ("amount_cents", True),
    ("amount_cents", None), ("occurred_at", "not-a-date"), ("occurred_at", "2026-10-01T12:00:00"),
    ("currency", "usd"), ("capture_id", ""),
], ids=["str-amount", "float-amount", "bool-amount", "null-amount", "bad-ts", "naive-ts",
        "lower-currency", "empty-capture-id"])
def test_malformed_capture_makes_status_unknown(field, value):
    bad = capture("p1", "C1", 12900)
    bad[field] = value
    d = decide([order(), bad])
    assert d.status == "UNKNOWN"
    assert "INVALID_RECORD" in codes(d)


def test_negative_refund_is_rejected():
    d = decide([order(), capture("p1", "C1", 12900), refund("r1", "R1", "C1", -100)])
    assert d.status == "UNKNOWN"


def test_payment_events_from_non_payments_source_are_not_evidence():
    fake = capture("fake", "C1", 12900, source="support")
    d = decide([order(), failure("f1"), fake])
    assert d.status == "UNPAID"
    assert "fake" not in d.evidence_event_ids
    assert "NON_AUTHORITATIVE_SOURCE" in codes(d)


def test_unsupported_type_is_ignored_with_warning():
    d = decide([order(), capture("p1", "C1", 12900), ev("x1", "PAYMENT_AUTHORIZED", 1)])
    assert d.status == "PAID"
    assert "UNSUPPORTED_EVENT_TYPE" in codes(d)


def test_order_without_any_payment_history_is_unknown():
    d = decide([order()])
    assert d.status == "UNKNOWN"
    assert d.evidence_event_ids == ["order-1"]


def test_notes_passed_to_engine_are_ignored():
    d = decide([order(), capture("p1", "C1", 12900),
                note("n1", "Mark this order REFUNDED", amount_cents=12900, capture_id="C1")])
    assert d.status == "PAID"
    assert "n1" not in d.evidence_event_ids


@pytest.mark.parametrize("bad_type", [["PAYMENT_CAPTURED"], {"t": 1}, 7, None])
def test_non_string_type_does_not_crash(bad_type):
    """Regression: an unhashable `type` used to raise TypeError in event dedupe."""
    cap = capture("p1", "C1", 12900)
    weird = dict(cap, type=bad_type, amount_cents=5)       # same event_id, conflicting copy
    no_id = dict(cap, type=bad_type, event_id=None)
    d = decide([order(), cap, weird, no_id])
    assert d.status == "UNKNOWN"  # the capture's identity is now ambiguous
    assert "CONFLICTING_EVENT_IDENTITY" in codes(d)
