"""JSONL loading. Never raises on bad content; only on an unreadable file."""
from __future__ import annotations

import json
from pathlib import Path

from .models import BadLine, LoadResult, Record


def parse_jsonl_lines(lines: list[str] | tuple[str, ...]) -> LoadResult:
    result = LoadResult()
    for number, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as exc:
            result.bad_lines.append(BadLine(number, f"invalid JSON: {exc}", text))
            continue
        if not isinstance(obj, dict):
            result.bad_lines.append(BadLine(number, "line is not a JSON object", text))
            continue
        result.records.append(Record(obj, number))
    return result


def load_jsonl(path: str | Path) -> LoadResult:
    """Read a JSONL file. Raises OSError if the file cannot be read."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_jsonl_lines(handle.read().splitlines())
