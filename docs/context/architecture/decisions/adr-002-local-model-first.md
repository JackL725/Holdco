# ADR-002: A local model first, and the product adapts to it

Status: Accepted | Date: 2026-10-20

## Context
Agents run on the owner's GPU (default `gpt-oss:20b` on Ollama, about 14 GB of VRAM; an RTX 5090 serves about 200
tokens per second). A 20-30B local model is far weaker than frontier cloud models: it invents numbers, repeats
calls, wraps tool calls oddly, fills optional fields with junk and drifts on long prompts. Every real-model run in
Phases 0.5 to 4 found behaviour the scripted test model never shows.

## Decision
- Holdco is built for the small model rather than waiting for a bigger one. Prompts follow one short skeleton per
  preset (mission, done, workflow, at most about 7 rules); tool belts stay at or under 18 tools for executors and 24
  for the CEO; outside services' tools load on demand with `find_tool`.
- Code does what code can do: gathering numbers for briefs, checking claims against records, verifying finished
  work, deduplicating questions, scheduling. The model writes and decides within those rails.
- Tool calls are recovered generously (text calls, wrapped calls, aliases, coercion of unambiguous values) and
  refused precisely (repair messages that say what to send). Every push-back happens once, so a model that cannot
  comply ends `partial` instead of looping.
- Any OpenAI-compatible server works too; a small model and an embedding model are optional helpers.
- Holdco Bench (`python -m holdco.bench`) measures a model on Holdco's own work, so the owner picks by score.

## Alternatives considered
- Requiring a cloud model: better results, but costs per token, sends company data out and contradicts the
  product's promise.
- Fine-tuning a model for Holdco: out of scope for a stdlib app; the Bench is the first step toward evidence.

## Consequences
- Each phase ends with a real-model check in a sandbox, and its findings become replay tests
  (`tests/test_phase2_realmodel.py`, `tests/test_phase3_realmodel.py`).
- Bench scores of one model vary by several scenarios between runs on unchanged code (gpt-oss:20b: 22-29 of 33);
  read the pattern across runs and never tune to a single run.
