"""Event store and the three investigation tools.

The tools return plain JSON-like dicts, as a remote service would. They do no
financial reasoning; they only scope records to exactly one (tenant, order).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import NOTE, ORDER_CREATED, LoadResult, Record

TOOL_NAMES = ("get_order", "get_payment_events", "get_support_notes")


class ToolError(Exception):
    """A tool call failed (service error, bad response, ...)."""


class ToolTimeout(ToolError):
    """A tool call did not answer within its time budget."""


def _key(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class EventStore:
    def __init__(self, loaded: LoadResult):
        self.loaded = loaded
        self._by_order: dict[tuple[str, str], list[Record]] = defaultdict(list)
        # (tenant, event_id) -> order_ids using that event id
        self._event_orders: dict[tuple[str, str], set[str]] = defaultdict(set)
        for rec in loaded.records:
            tenant, order = _key(rec.get("tenant_id")), _key(rec.get("order_id"))
            if tenant is None or order is None:
                continue  # cannot be attributed to any order
            self._by_order[(tenant, order)].append(rec)
            eid = _key(rec.event_id)
            if eid is not None:
                self._event_orders[(tenant, eid)].add(order)

    def orders(self) -> list[tuple[str, str]]:
        return sorted(self._by_order)

    def records_for(self, tenant_id: str, order_id: str) -> list[Record]:
        # Exact (tenant, order) match: never merges tenants that share order IDs.
        return list(self._by_order.get((tenant_id, order_id), ()))

    def event_ids_reused_elsewhere(self, tenant_id: str, order_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for rec in self.records_for(tenant_id, order_id):
            eid = _key(rec.event_id)
            if eid is None:
                continue
            others = self._event_orders[(tenant_id, eid)] - {order_id}
            if others:
                out[eid] = sorted(others)
        return out

    def corrupt_lines_mentioning(self, tenant_id: str, order_id: str) -> list[int]:
        """Unparseable lines that textually mention this tenant and order."""
        return [
            bad.line
            for bad in self.loaded.bad_lines
            if f'"{tenant_id}"' in bad.raw and f'"{order_id}"' in bad.raw
        ]


def _serialize(records: list[Record]) -> list[dict[str, Any]]:
    return [{"line": r.line, "record": dict(r.data)} for r in records]


@dataclass
class FaultPlan:
    """Simulated outages: tool name -> number of leading calls affected."""

    failures: dict[str, int] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)
    hang_seconds: float = 2.0


class InvestigationTools:
    def __init__(self, store: EventStore, faults: FaultPlan | None = None):
        self.store = store
        self.faults = faults or FaultPlan()
        self.call_counts: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    # -- the three public tool interfaces ---------------------------------
    def get_order(self, tenant_id: str, order_id: str) -> dict[str, Any]:
        self._maybe_fault("get_order")
        all_records = self.store.records_for(tenant_id, order_id)
        return {
            "tenant_id": tenant_id,
            "order_id": order_id,
            "found": bool(all_records),
            "records": _serialize([r for r in all_records if r.type == ORDER_CREATED]),
            "event_ids_reused_by_other_orders": self.store.event_ids_reused_elsewhere(tenant_id, order_id),
            "corrupt_lines_mentioning_order": self.store.corrupt_lines_mentioning(tenant_id, order_id),
        }

    def get_payment_events(self, tenant_id: str, order_id: str) -> dict[str, Any]:
        """Everything that is neither the order definition nor a note,
        including unsupported or untyped records so they can be reported."""
        self._maybe_fault("get_payment_events")
        records = [
            r for r in self.store.records_for(tenant_id, order_id) if r.type not in (ORDER_CREATED, NOTE)
        ]
        return {"tenant_id": tenant_id, "order_id": order_id, "records": _serialize(records)}

    def get_support_notes(self, tenant_id: str, order_id: str) -> dict[str, Any]:
        self._maybe_fault("get_support_notes")
        records = [r for r in self.store.records_for(tenant_id, order_id) if r.type == NOTE]
        return {"tenant_id": tenant_id, "order_id": order_id, "records": _serialize(records)}

    def by_name(self, name: str) -> Callable[[str, str], dict[str, Any]]:
        if name not in TOOL_NAMES:
            raise KeyError(name)
        return getattr(self, name)

    # -- fault injection ----------------------------------------------------
    def _maybe_fault(self, name: str) -> None:
        with self._lock:
            self.call_counts[name] += 1
            n = self.call_counts[name]
        if n <= self.faults.timeouts.get(name, 0):
            time.sleep(self.faults.hang_seconds)
            raise ToolTimeout(f"{name} hung (simulated)")
        if n <= self.faults.failures.get(name, 0):
            raise ToolError(f"{name} returned 503 Service Unavailable (simulated)")
