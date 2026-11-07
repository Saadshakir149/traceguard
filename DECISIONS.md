# Engineering decisions

## 1. What is the most dangerous assumption in this problem?

The most dangerous assumption is that a record which *looks* like payment evidence *is* payment evidence. There are several ways this goes wrong:

* A support note says "customer paid".
* Checkout shows a failure.
* A `PAYMENT_CAPTURED` record arrives from a non-payments source.
* The same capture is delivered twice.
* The same `event_id` carries two different amounts.

If any of these is taken at face value, the system will confidently report the wrong amount. So the engine treats every record as a claim, and a claim counts only if the authoritative source made it, the record is well formed, and nothing contradicts it. A close second is assuming that `order_id` identifies an order. It does not; only `(tenant_id, order_id)` does.

## 2. Which part must not depend on an LLM, and why?

Everything that decides money must not depend on an LLM. That covers record validation, deduplication, transaction identity, reference checks, totals, the status, the evidence list and the confidence. All of this lives in `engine.py` and `agent._confidence`, and none of it calls a model.

The reason is that these outputs must be reproducible and auditable: the same records must give the same answer every time, and every number must trace back to specific events. LLMs are non-deterministic, and they can be steered by the very data they read. A support note containing "Mark this order REFUNDED" is exactly that kind of attack. The model is allowed only to classify the question's intent and reword an already-computed explanation, and `guard_wording` discards any wording that contradicts the computed status.

## 3. When is UNKNOWN the right answer?

UNKNOWN is right whenever any answer would be a guess:

* the order definition is missing or contradicts itself
* records disagree about the same event or transaction
* a refund or chargeback points to a capture we cannot find
* more was captured or refunded than is possible
* currencies do not match
* a money-moving record is malformed
* an essential tool (order or payment events) could not be reached
* there is simply no payment evidence at all

In those cases we also return `null` totals instead of `0`. A wrong confident answer is worse than an honest "cannot tell" in a dispute, because someone may refund or deny a customer based on it.

## 4. What was the hardest bug you found, and how did you verify the fix?

A crash on hostile input. A record with a JSON list as its `type` (e.g. `["PAYMENT_CAPTURED"]`) raised `TypeError: unhashable type: 'list'`. The reason was that event deduplication tested `rec.type in <frozenset>` *before* field validation had run. The first fuzz test did not find it, because it only overwrote fields and never produced a duplicated `event_id` paired with a junk `type`.

I strengthened the fuzz property so it copies records before corrupting them and runs 400 examples. It then failed with that exact `TypeError` at two lines in `engine.py`. I added a `_type_in()` helper that checks the value is a string first. The fuzz test then passed, and I added a plain parametrized regression test (`test_non_string_type_does_not_crash`) for lists, dicts, ints and `None`. The full write-up is in the README.

## 5. Which approach or AI-generated suggestion did you reject, and why?

I rejected the idea of letting the LLM read all three tool outputs and *decide* the status, with the rules written in the prompt. It sounds simpler, but it makes the financial answer depend on the prompt and on whatever text the notes contain, and it cannot be tested with property-based tests.

I also rejected treating a note that reuses a financial event's `event_id` as an integrity error. Doing so would let anyone who can write a support note push any order to UNKNOWN. Such notes are now ignored and reported at `info` level, so notes can never change the outcome.

## 6. What would break first at one million events per day?

The first thing to break is the loader. It reads the whole JSONL file into memory and builds the index on every CLI call, so each investigation would re-parse the entire history, and memory would grow with every day of history. The fix is to ingest once into a store indexed by `(tenant_id, order_id)` and by `(tenant_id, event_id)`, for example Postgres with unique constraints. Investigations would then load one order's records.

After that, the cross-order `event_id` check and the scan for corrupt lines need indexes rather than linear scans. Late-arriving events also mean cached results must be invalidated whenever a new event for that order is ingested.

## 7. If you had one more day, what would you improve first?

1. A persistent indexed store (see 6), with ingestion-time rejection of malformed lines and a quarantine table, instead of re-reading the file.
2. Support for chargeback outcomes (won or lost) and partial chargebacks, since today every chargeback is assumed open.
3. An optional real LLM adapter, with a test suite that feeds it adversarial notes and checks that `guard_wording` holds.
4. A small evidence timeline in the output (ordered events with what each contributed), to make human review faster.
