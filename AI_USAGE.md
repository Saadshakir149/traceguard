# AI usage

## Tools used

* **Claude Code** (Anthropic, Claude Opus model) in VS Code. I used it to scaffold the package, generate the dataset script, write the tests and draft the documentation. I reviewed, ran and adjusted all of it.

## One useful suggestion

Use **hypothesis** to generate random but *valid* order histories, then apply a transformation and assert that the decision does not change. The transformations are: shuffle, insert duplicates, add or change notes, and add another tenant's events. This one generator made five metamorphic tests cheap to write. It also led to a model-based test that checks the engine against a separate, very direct implementation of the status rules.

## One suggestion I corrected or rejected

* **Corrected:** the first fuzz test it wrote passed while the engine could still crash on a list-valued `type`. The test never created duplicate event IDs, and duplicates are what reach the unguarded code path. I made the test copy records before corrupting them and raised the example count. The test then failed, and I fixed the engine (see README, "A failing test we hit").
* **Rejected:** letting the language model read the tool outputs and decide the status. Financial state is computed only by the deterministic engine. The model can only classify intent and reword text, and its wording is checked against the computed status.
