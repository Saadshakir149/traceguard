"""TraceGuard command line entry point.

Example:
    python run.py --events data/events.jsonl --tenant T1 --order O-201 \
        --question "Was this order paid?"
"""
from __future__ import annotations

import argparse
import json
import sys

from traceguard.agent import RetryPolicy, investigate
from traceguard.loader import load_jsonl
from traceguard.tools import TOOL_NAMES, EventStore, FaultPlan, InvestigationTools


def _fault_arg(value: str) -> tuple[str, int]:
    name, _, count = value.partition("=")
    if name not in TOOL_NAMES:
        raise argparse.ArgumentTypeError(f"unknown tool {name!r}; choose from {', '.join(TOOL_NAMES)}")
    try:
        n = int(count) if count else 1
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad count in {value!r}") from None
    return name, n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Investigate the financial state of one order.")
    p.add_argument("--events", required=True, help="Path to a JSONL events file")
    p.add_argument("--tenant", required=True)
    p.add_argument("--order", required=True)
    p.add_argument("--question", default="What is the status of this order?")
    p.add_argument("--simulate-failure", action="append", type=_fault_arg, default=[], metavar="TOOL[=N]",
                   help="Make the first N calls of TOOL fail (demo of bounded retry)")
    p.add_argument("--simulate-timeout", action="append", type=_fault_arg, default=[], metavar="TOOL[=N]",
                   help="Make the first N calls of TOOL hang past the timeout")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--tool-timeout", type=float, default=1.0, help="Seconds per tool attempt")
    p.add_argument("--trace", action="store_true", help="Print the tool-call trace to stderr")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.tenant.strip() or not args.order.strip():
        print(json.dumps({"error": "tenant and order must be non-empty"}))
        return 2
    if not 1 <= args.max_attempts <= 10:
        print(json.dumps({"error": "--max-attempts must be between 1 and 10"}))
        return 2
    try:
        loaded = load_jsonl(args.events)
    except OSError as exc:
        print(json.dumps({"error": f"cannot read events file: {exc}"}))
        return 2

    faults = FaultPlan(
        failures=dict(args.simulate_failure),
        timeouts=dict(args.simulate_timeout),
        hang_seconds=args.tool_timeout + 0.5,
    )
    tools = InvestigationTools(EventStore(loaded), faults)
    policy = RetryPolicy(max_attempts=args.max_attempts, timeout_seconds=args.tool_timeout)
    inv = investigate(tools, args.tenant, args.order, args.question, policy=policy)

    if args.trace:
        print(f"[trace] intent={inv.intent}; input: {len(loaded.records)} records, "
              f"{len(loaded.bad_lines)} unparseable lines", file=sys.stderr)
        for call in inv.trace:
            print(f"[trace] {call.tool}: " + " | ".join(call.attempts), file=sys.stderr)
    print(json.dumps(inv.result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
