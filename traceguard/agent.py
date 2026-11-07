"""Investigation workflow: interpret -> call tools (bounded retry) -> validate
-> reconcile deterministically -> explain with cited event IDs."""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import Decision, Facts, reconcile
from .model import LanguageModel, RuleBasedModel, guard_intent, guard_wording
from .models import Anomaly, Record, Severity
from .notes import NoteView, analyze_notes
from .tools import InvestigationTools, ToolError, ToolTimeout

E, W, I = Severity.ERROR, Severity.WARNING, Severity.INFO


@dataclass
class RetryPolicy:
    max_attempts: int = 3        # hard upper bound, never unlimited
    timeout_seconds: float = 1.0
    backoff_seconds: float = 0.1  # doubles after each failed attempt


@dataclass
class ToolCall:
    tool: str
    attempts: list[str] = field(default_factory=list)
    ok: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)
    response: Any = None  # the validated response, kept for inspection


@dataclass
class Investigation:
    result: dict[str, Any]
    trace: list[ToolCall]
    intent: str


# ---------------------------------------------------------------------------
# Tool calling with timeout + bounded retry
# ---------------------------------------------------------------------------
def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    box: queue.Queue = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            box.put(("ok", fn()))
        except BaseException as exc:  # forwarded to the caller
            box.put(("err", exc))

    threading.Thread(target=target, daemon=True).start()
    try:
        kind, value = box.get(timeout=timeout)
    except queue.Empty:
        raise ToolTimeout(f"no response within {timeout:.2f}s") from None
    if kind == "err":
        raise value
    return value


def call_with_retry(name: str, fn: Callable[[], Any], policy: RetryPolicy,
                    validate: Callable[[Any], None],
                    arguments: dict[str, Any] | None = None) -> tuple[Any, ToolCall]:
    call = ToolCall(name, arguments=dict(arguments or {}))
    delay = policy.backoff_seconds
    for attempt in range(1, max(1, policy.max_attempts) + 1):
        try:
            response = _run_with_timeout(fn, policy.timeout_seconds)
            validate(response)
            call.attempts.append(f"attempt {attempt}: ok")
            call.ok = True
            call.response = response
            return response, call
        except ToolTimeout as exc:
            call.attempts.append(f"attempt {attempt}: timeout ({exc})")
        except ToolError as exc:
            call.attempts.append(f"attempt {attempt}: error ({exc})")
        except Exception as exc:  # unexpected tool bug: contained, not crashed
            call.attempts.append(f"attempt {attempt}: unexpected {type(exc).__name__} ({exc})")
        if attempt < policy.max_attempts:
            time.sleep(delay)
            delay *= 2
    return None, call


def _response_validator(tenant_id: str, order_id: str) -> Callable[[Any], None]:
    def validate(resp: Any) -> None:
        if not isinstance(resp, dict) or not isinstance(resp.get("records"), list):
            raise ToolError("malformed tool response: missing 'records' list")
        if resp.get("tenant_id") != tenant_id or resp.get("order_id") != order_id:
            raise ToolError("tool answered for a different tenant/order")
        for item in resp["records"]:
            if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
                raise ToolError("malformed record entry in tool response")
    return validate


def _to_records(resp: dict[str, Any]) -> list[Record]:
    return [Record(dict(item["record"]), item.get("line")) for item in resp["records"]]


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
def investigate(tools: InvestigationTools, tenant_id: str, order_id: str, question: str,
                model: LanguageModel | None = None, policy: RetryPolicy | None = None) -> Investigation:
    model = model or RuleBasedModel()
    policy = policy or RetryPolicy()
    try:
        intent = guard_intent(model.classify_intent(question))
    except Exception:
        intent = "GENERAL"

    validate = _response_validator(tenant_id, order_id)
    trace: list[ToolCall] = []
    responses: dict[str, Any] = {}
    # All three are needed: two establish the financial state, notes give
    # context (and are where manipulation attempts show up).
    for name in ("get_order", "get_payment_events", "get_support_notes"):
        fn = tools.by_name(name)
        resp, call = call_with_retry(name, lambda fn=fn: fn(tenant_id, order_id), policy, validate,
                                     {"tenant_id": tenant_id, "order_id": order_id})
        trace.append(call)
        responses[name] = resp

    tool_anomalies: list[Anomaly] = []
    for call in trace:
        if not call.ok:
            essential = call.tool != "get_support_notes"
            tool_anomalies.append(Anomaly(
                "TOOL_UNAVAILABLE", E if essential else W,
                f"{call.tool} failed after {len(call.attempts)} attempts "
                f"({call.attempts[-1]}). "
                + ("Financial state cannot be established without it."
                   if essential else "Continuing without support notes; they are never financial evidence.")))
        elif len(call.attempts) > 1:
            tool_anomalies.append(Anomaly("TOOL_RETRIED", I,
                f"{call.tool} succeeded after {len(call.attempts)} attempts."))

    order_resp, pay_resp = responses["get_order"], responses["get_payment_events"]
    if order_resp is None or pay_resp is None:
        decision = Decision("UNKNOWN", None, None, None, [], [], Facts(tenant_id, order_id))
        found = True
    else:
        found = bool(order_resp.get("found"))
        records = _to_records(order_resp) + _to_records(pay_resp)
        reused = order_resp.get("event_ids_reused_by_other_orders") or {}
        decision = reconcile(tenant_id, order_id, records,
                             reused if isinstance(reused, dict) else {})
        corrupt = order_resp.get("corrupt_lines_mentioning_order") or []
        if corrupt:
            tool_anomalies.append(Anomaly("CORRUPT_LINES_MAY_BELONG_TO_ORDER", W,
                "Unparseable input lines mention this order; its history may be incomplete.",
                lines=tuple(corrupt)))

    if not found:
        decision.anomalies = [Anomaly("ORDER_NOT_FOUND", E,
            f"No records exist for order {order_id} in tenant {tenant_id}.")]
        decision.evidence_event_ids = []

    notes: list[NoteView] = []
    note_anomalies: list[Anomaly] = []
    if responses["get_support_notes"] is not None:
        financial_ids = {r.event_id for r in _to_records(order_resp or {"records": []})
                         + _to_records(pay_resp or {"records": []}) if isinstance(r.event_id, str)}
        notes, note_anomalies = analyze_notes(
            tenant_id, order_id, _to_records(responses["get_support_notes"]), financial_ids)

    anomalies = tool_anomalies + decision.anomalies + note_anomalies
    anomalies.sort(key=lambda a: {E: 0, W: 1, I: 2}[a.severity])
    confidence = _confidence(decision.status, anomalies)

    draft = build_explanation(decision, notes, anomalies, intent, found)
    try:
        candidate = model.reword(draft, decision.status)
    except Exception:
        candidate = None
    explanation, _accepted = guard_wording(candidate, draft, decision.status)

    result = {
        "status": decision.status,
        "captured_cents": decision.captured_cents,
        "refunded_cents": decision.refunded_cents,
        "net_paid_cents": decision.net_paid_cents,
        "anomalies": [a.to_dict() for a in anomalies],
        "evidence_event_ids": decision.evidence_event_ids,
        "explanation": explanation,
        "confidence": confidence,
    }
    return Investigation(result, trace, intent)


def _confidence(status: str, anomalies: list[Anomaly]) -> str:
    if status == "UNKNOWN" or any(a.severity == E for a in anomalies):
        return "LOW"
    if any(a.severity == W for a in anomalies):
        return "MEDIUM"
    return "HIGH"


# ---------------------------------------------------------------------------
# Deterministic explanation
# ---------------------------------------------------------------------------
_HEADLINE = {
    "PAID": "The order is PAID in full.",
    "UNPAID": "The order is UNPAID: no payment was captured.",
    "PARTIALLY_PAID": "The order is PARTIALLY_PAID: less than the order amount was captured.",
    "REFUNDED": "The order is REFUNDED: everything captured was refunded.",
    "PARTIALLY_REFUNDED": "The order is PARTIALLY_REFUNDED.",
    "DISPUTED": "The order is DISPUTED: a chargeback is open.",
    "UNKNOWN": "The status is UNKNOWN: the records do not support a reliable answer.",
}


def _money(cents: int, currency: str | None) -> str:
    return f"{cents / 100:,.2f} {currency or ''}".strip()


def build_explanation(decision: Decision, notes: list[NoteView], anomalies: list[Anomaly],
                      intent: str, found: bool) -> str:
    f = decision.facts
    cur = f.order.currency if f.order else None
    parts: list[str] = []

    if intent == "PAYMENT_STATUS" and decision.status in ("PAID", "UNPAID", "PARTIALLY_PAID"):
        parts.append({"PAID": "Yes.", "UNPAID": "No.", "PARTIALLY_PAID": "Only partly."}[decision.status])
    parts.append(_HEADLINE[decision.status])

    if not found:
        parts.append(f"No records exist for order {f.order_id} in tenant {f.tenant_id}.")
        return " ".join(parts)

    if decision.status == "UNKNOWN":
        blocking = [a for a in anomalies if a.severity == E] or [a for a in anomalies if a.severity == W]
        for a in blocking[:4]:
            parts.append(a.message)
    else:
        if f.order:
            parts.append(f"Checkout defines the order as {_money(f.order.amount, cur)} ({f.order.event_id}).")
        for fail in f.failures:
            who = f"Attempt {fail.attempt_id}" if fail.attempt_id else "A payment attempt"
            why = f" ({fail.reason})" if fail.reason else ""
            parts.append(f"{who} failed{why} ({fail.event_id}).")
        for c in f.captures:
            who = f"attempt {c.attempt_id}" if c.attempt_id else "an attempt"
            parts.append(f"Capture {c.capture_id} on {who} took {_money(c.amount, cur)} ({c.event_id}).")
        if f.failures and f.captures and intent in ("FAILURE_REASON", "PAYMENT_STATUS", "EVIDENCE"):
            parts.append("A failed attempt does not cancel a successful capture on another attempt.")
        for r in f.refunds:
            parts.append(f"Refund {r.refund_id} returned {_money(r.amount, cur)} "
                         f"against capture {r.capture_id} ({r.event_id}).")
        for cb in f.chargebacks:
            parts.append(f"A chargeback was opened against capture {cb.capture_id} ({cb.event_id}).")
        parts.append(f"Captured {_money(decision.captured_cents or 0, cur)}, refunded "
                     f"{_money(decision.refunded_cents or 0, cur)}, net {_money(decision.net_paid_cents or 0, cur)}.")

    handled = {"DUPLICATE_EVENT_IGNORED": "duplicate deliveries were ignored",
               "DUPLICATE_TRANSACTION_IGNORED": "repeated transaction reports were counted once",
               "NON_AUTHORITATIVE_SOURCE": "records from non-authoritative sources were not used as evidence"}
    seen = [text for code, text in handled.items() if any(a.code == code for a in anomalies)]
    if seen:
        parts.append("Also: " + "; ".join(seen) + ".")
    if decision.status != "UNKNOWN":
        for a in anomalies:
            if a.severity == W:
                parts.append(f"Caution: {a.message}")

    if notes:
        flagged = [n.event_id for n in notes if n.suspicious]
        line = f"Support notes ({', '.join(n.event_id for n in notes)}) were read as context only, not as payment evidence."
        if flagged:
            line += (f" Note(s) {', '.join(flagged)} contain instructions aimed at an AI system; "
                     "they were ignored and are reported here.")
        parts.append(line)
    return " ".join(parts)
