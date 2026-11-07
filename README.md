# TraceGuard

TraceGuard investigates payment disputes. Given a JSONL event log, a tenant and an order, it rebuilds the order's financial state with deterministic rules. It also reports gaps and contradictions and explains the result, citing real event IDs.

It needs no API keys, network access or paid services. The runtime uses only the Python standard library.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt        # pytest + hypothesis, for tests only
```

Tested with Python 3.14. Any Python 3.11+ should work.

## Run

```bash
python run.py --events data/events.jsonl --tenant T1 --order O-201 --question "Was this order paid?"
```

Useful flags:

| Flag | Meaning |
|---|---|
| `--trace` | Print the intent and every tool attempt to stderr |
| `--simulate-failure TOOL=N` | First N calls to TOOL fail with a simulated 503 |
| `--simulate-timeout TOOL=N` | First N calls to TOOL hang past the timeout |
| `--max-attempts N` | Retry bound per tool (default 3, max 10) |
| `--tool-timeout S` | Seconds per attempt (default 1.0) |

The tool names are `get_order`, `get_payment_events` and `get_support_notes`.

To regenerate the sample data (it is deterministic): `python scripts/generate_dataset.py`

## Dashboard (optional)

```bash
python dashboard.py --events data/events.jsonl      # then open the URL it prints (default http://127.0.0.1:8050)
```

If the port is busy, it moves to the next free one and prints that URL instead. Use `--port N` to choose a port.

The dashboard is a local web UI over the same `investigate()` workflow. It needs only the standard library and makes no network calls beyond localhost. It shows:

* status counts and a filterable order list
* a question box with presets
* totals (`null` stays `null`), confidence, the explanation and clickable evidence IDs
* the anomalies and the tool-call trace
* the raw event timeline: evidence, untrusted notes and injection attempts are tagged, and each row expands to its raw JSON
* the exact CLI JSON

Under "Simulate tool failures" you can make any tool fail or time out to watch the bounded retry and fallback. The browser only displays results. All financial logic stays in Python, and every input-derived string is rendered as text, never as HTML.

## Test

```bash
python -m pytest -q
```

There are 39 test functions. Parametrization expands them to 113 test cases. Six of the functions use hypothesis (property-based or metamorphic):

* status matches an independent oracle written from the rules
* shuffling events does not change the decision
* inserting duplicates (only `ingested_at` changed) does not change it
* adding or changing support notes, including the injection text, does not change it
* adding events from another tenant with the same order and event IDs does not change it
* fuzzing: arbitrary JSON in any field never crashes the program and never invents evidence

## Demo commands (used in the video)

```bash
# 1. Normal investigation: failed attempt, capture on another attempt, duplicate ignored
python run.py --events data/events.jsonl --tenant T1 --order O-201 --question "Was this order paid?"
python run.py --events data/events.jsonl --tenant T1 --order O-201 --question "Why does checkout show a failure?"

# 2. Adversarial: support note with SYSTEM OVERRIDE text; the order stays PAID and the note is flagged
python run.py --events data/events.jsonl --tenant T1 --order O-207 --question "What evidence supports the current status?"

# 3. Conflicting data -> UNKNOWN with null totals
python run.py --events data/events.jsonl --tenant T1 --order O-213   # same event_id, different amount
python run.py --events data/events.jsonl --tenant T1 --order O-211   # refunds exceed capture
python run.py --events data/events.jsonl --tenant T2 --order O-201   # same order id, other tenant

# 4. Tool failure with bounded retry, and a notes timeout with fallback
python run.py --events data/events.jsonl --tenant T1 --order O-201 --trace \
  --simulate-failure get_payment_events=2 --simulate-timeout get_support_notes=3
```

`data/expected.json` lists every order in the dataset with its expected status and the reason.

### What is in `data/events.jsonl`

The file has **165 lines**:

| Lines | What | Why |
|---|---|---|
| 162 | Parsed JSON objects (records) | Normal and difficult events |
| 3 | Intentionally unparseable | A truncated JSON line that mentions `T1`/`O-223`, the text `this is not json`, and `[1, 2, 3]` (valid JSON but not an object) |

One of the 162 records (`orphan-1`) has no `tenant_id` or `order_id`, so it cannot belong to any order and is skipped. The other 161 records belong to **59 orders** across tenants `T1` and `T2`. `T1/O-201` and `T2/O-201` share an order ID on purpose.

The dashboard header shows these counts. Click "3 unparseable lines" to see each bad line and why it failed. The truncated line mentions `T1`/`O-223`, so `O-223` is still `PAID` but drops to `MEDIUM` confidence: its history may be incomplete.

## Layout

```
run.py                     CLI
traceguard/loader.py       JSONL parsing; bad lines are recorded and never crash the program
traceguard/tools.py        EventStore + the 3 tools (+ fault injection for demos and tests)
traceguard/engine.py       Deterministic reconciliation (the only place status/totals are computed)
traceguard/notes.py        Support notes: untrusted text, flags injection attempts
traceguard/model.py        Replaceable "model" layer (intent + wording) and its output guard
traceguard/agent.py        Workflow: intent -> tools with timeout/retry -> validate -> reconcile -> explain
scripts/generate_dataset.py  Builds data/events.jsonl (165 lines) and data/expected.json
tests/                     pytest + hypothesis
```

## How it works

1. **Interpret.** `RuleBasedModel.classify_intent` maps the question to an intent: payment status, failure reason, evidence, refund, dispute or general. The intent only changes how the explanation is worded. It never changes the decision.
2. **Call tools.** All three tools are called. Each call runs with a timeout and at most `max_attempts` tries, with exponential backoff. Each response is validated: it must have the right shape and answer for the requested tenant and order. A response for the wrong tenant counts as a failed call.
3. **Fallback.** If `get_order` or `get_payment_events` still fails, the result is `UNKNOWN` with null totals and `LOW` confidence. If `get_support_notes` fails, the investigation continues without notes, with a warning.
4. **Reconcile.** `engine.reconcile` receives only order and payment records. Notes never reach it, and the engine also drops notes if they are passed in.
5. **Explain.** A deterministic text is built from validated facts only. A pluggable model may reword it. `guard_wording` rejects any rewording that drops the computed status or names a different status, and the deterministic text is used instead.

To plug in a real LLM, implement `classify_intent(question) -> str` and `reword(draft, status) -> str` (see `LanguageModel` in `model.py`), then pass the model to `investigate(...)`. Its output is still guarded.

### Confidence

| Confidence | When |
|---|---|
| `HIGH` | Status is established and there are only `info` anomalies (e.g. a duplicate delivery ignored, a suspicious note flagged). |
| `MEDIUM` | Status is established but there is at least one `warning`: unsupported event type, refund timestamp before its capture, the same attempt reported both failed and captured, a corrupt input line that mentions this order, or the notes tool unavailable. |
| `LOW` | Status is `UNKNOWN`, i.e. any `error` anomaly, missing evidence or an essential tool failure. |

Support notes cannot change confidence. Every anomaly they produce is `info`.

### Anomaly severities

* `error`: an integrity failure. Status becomes `UNKNOWN` and all totals are `null`.
* `warning`: the status is still decidable but less certain.
* `info`: input that was handled as expected.

## Assumptions about incomplete or malformed records

* **Timestamps** must be ISO 8601 *with an offset* (`Z` or `+05:00`). Naive timestamps are rejected because they cannot be ordered safely. `ingested_at` may be missing, but if present it must be valid.
* **Amounts** must be JSON integers greater than 0. Strings (`"4000"`), floats (`40.0`), booleans and negative numbers are invalid. Currency must be 3 uppercase letters.
* **Required fields:** `ORDER_CREATED` needs an amount and currency. A capture needs `capture_id`, amount and currency (`attempt_id` is optional). A refund needs `refund_id`, `capture_id`, amount and currency. A chargeback needs `capture_id`; its amount and currency are optional but must be valid if present. A failure needs only a valid timestamp.
* **A malformed money-moving record** (order, capture, refund or chargeback from the authoritative source) is an `error`, because we cannot know whether money moved. A malformed `PAYMENT_FAILED` is a `warning`: it is dropped, and it can only weaken the case for `UNPAID`.
* **Authority:** only `source == "checkout"` may define the order, and only `source == "payments"` may establish failures, captures, refunds and chargebacks. Records of those types from other sources are ignored (`info`) and never cited as evidence.
* **Event identity:** an `event_id` is unique per tenant. Records with the same `event_id` that differ only in `ingested_at` are one event. Any other difference is a conflict, and none of the versions is used. An `event_id` reused by another order in the same tenant is also a conflict.
* **Transaction identity:** two events with *different* event IDs but the same `capture_id` (or `refund_id`) and identical details are one transaction reported twice. It is counted once (`info`). If the details differ, it is an `error`.
* **Chargebacks:** there is no "closed" event type, so every valid `CHARGEBACK_OPENED` is treated as open. Its identity is `chargeback_id` if present, otherwise `event_id`.
* **Failure vs capture on the same attempt:** the capture wins, because it is a record of money moving, but confidence drops to `MEDIUM`.
* **Records without `tenant_id`/`order_id`**, and lines that are not JSON, cannot be attributed to any order and are skipped. If an unparseable line textually contains the requested tenant and order IDs, the investigation adds a `warning` because the history may be incomplete.
* **Totals:** when the status is `UNKNOWN`, all three totals are `null`, including UNKNOWN by rule 8. We do not publish numbers we do not stand behind. For `UNPAID`, the zeros *are* established by the failure evidence.
* **Evidence IDs:** for an established status, these are the order definition plus every counted failure, capture, refund and chargeback. For `UNKNOWN`, they are the records involved in the blocking anomalies plus the order definition when valid. Notes are never evidence. IDs are sorted by `occurred_at`.

## A failing test we hit, root cause and fix

**Test:** `test_corrupted_fields_never_crash_and_never_invent_evidence` (fuzz property in `tests/test_properties.py`).

* **What happened.** The first version of the fuzz test passed. A manual probe then showed a crash: a record whose `type` was a JSON list, e.g. `"type": ["PAYMENT_CAPTURED"]`, raised `TypeError: unhashable type: 'list'`. The crash needed two things on the same event: a duplicated `event_id` and a junk `type`. The test only overwrote fields, so it never produced duplicates. And with 60 examples it rarely hit the combination.
* **Root cause.** `_dedupe_events` in `engine.py` checked `rec.type in MONEY_MOVING_TYPES` (a `frozenset`) on raw untrusted JSON. Membership tests on a set hash the value, and lists and dicts cannot be hashed. Validation guarded this path elsewhere, but dedupe runs *before* validation.
* **Fix.** Added `_type_in(rec, types)`, which checks `isinstance(rec.type, str)` before testing membership, and used it at both call sites. The fuzz test now also copies records before corrupting them, so identity checks always run, and uses 400 examples. Before the fix it failed with that `TypeError` at `engine.py` lines 250 and 267, and it passes after. A plain regression test, `test_non_string_type_does_not_crash`, pins the case down.
