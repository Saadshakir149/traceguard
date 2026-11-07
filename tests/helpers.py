"""Small builders so tests read like event timelines."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from traceguard.agent import RetryPolicy, investigate
from traceguard.engine import reconcile
from traceguard.models import LoadResult, Record
from traceguard.tools import EventStore, FaultPlan, InvestigationTools

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
T, O = "T1", "O-1"


def ts(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def ev(eid, etype, at=0, source=None, tenant=T, order=O, **fields):
    if source is None:
        source = {"ORDER_CREATED": "checkout", "NOTE": "support"}.get(etype, "payments")
    rec = {"tenant_id": tenant, "order_id": order, "event_id": eid, "source": source, "type": etype,
           "occurred_at": ts(at), "ingested_at": ts(at + 1)}
    rec.update(fields)
    return rec


def order(eid="order-1", amount=12900, cur="USD", **kw):
    return ev(eid, "ORDER_CREATED", 0, amount_cents=amount, currency=cur, **kw)


def capture(eid, cap_id, amount, attempt="A1", at=5, cur="USD", **kw):
    return ev(eid, "PAYMENT_CAPTURED", at, capture_id=cap_id, attempt_id=attempt,
              amount_cents=amount, currency=cur, **kw)


def failure(eid, attempt="A1", at=2, **kw):
    return ev(eid, "PAYMENT_FAILED", at, attempt_id=attempt, **kw)


def refund(eid, ref_id, cap_id, amount, at=50, cur="USD", **kw):
    return ev(eid, "REFUND_ISSUED", at, refund_id=ref_id, capture_id=cap_id,
              amount_cents=amount, currency=cur, **kw)


def chargeback(eid, cap_id, at=80, **kw):
    return ev(eid, "CHARGEBACK_OPENED", at, capture_id=cap_id, **kw)


def note(eid, text, at=30, **kw):
    return ev(eid, "NOTE", at, text=text, **kw)


def decide(records, tenant=T, order_id=O):
    return reconcile(tenant, order_id, [Record(r, i) for i, r in enumerate(records, 1)])


def tools_for(records, faults: FaultPlan | None = None) -> InvestigationTools:
    loaded = LoadResult(records=[Record(r, i) for i, r in enumerate(records, 1)])
    return InvestigationTools(EventStore(loaded), faults)


FAST = RetryPolicy(max_attempts=3, timeout_seconds=0.2, backoff_seconds=0.01)


def run(records, question="Was this order paid?", tenant=T, order_id=O, faults=None, model=None,
        policy=FAST):
    return investigate(tools_for(records, faults), tenant, order_id, question, model=model, policy=policy)


def codes(result_or_decision) -> list[str]:
    anomalies = (result_or_decision["anomalies"] if isinstance(result_or_decision, dict)
                 else [a.to_dict() for a in result_or_decision.anomalies])
    return [a["code"] for a in anomalies]
