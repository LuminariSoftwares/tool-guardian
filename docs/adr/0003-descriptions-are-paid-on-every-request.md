# 0003. Router descriptions are paid for on every request

**Status:** accepted (budget made explicit 2026-10-21)

## Context

The router exists to shrink the fixed per-request cost (0001), and its own tool definitions are that
cost. Better descriptions help a model pick the right tool and help directories such as Glama rate the
server, but every added word is re-sent on every request.

## Decision

- Token cost is measured, not guessed: `model_cost()` counts what the model is sent (name, description,
  schema) and leaves out `title`/`annotations`, which clients use for UI and permission prompts.
  `--selftest` reports it (with `tiktoken` when importable), and `bench/bench_tokens.py` re-measures the
  README's figures from recorded catalogues.
- Each description says, in this order and about one sentence each: what the tool is for, when a
  sibling tool is the better call, what it touches, and what comes back. Parameters carry `type`,
  `description`, `examples` and `default`.
- `tests/test_tool_definitions.py` pins the router's cost with no backends at 1,500 tokens or less, so a
  later description change that crosses it is a decision someone writes down, not drift.

## Alternatives

- **Terse one-line descriptions** (the 0.7.0 state, 942 tokens): cheaper, but a model has to guess when
  to search instead of list, and whether `call_tool` can write.
- **Long descriptions with worked examples:** clearer still, but would erase a meaningful part of the
  saving on a 32K window.

## Consequences

- The 2026-10-21 rewrite raised the cost from 942 to 1,478 tokens with no backends, and on the
  seven-server bench from 1,209 to 1,790 (94.0 % → 91.1 % saved; 3.7 % → 5.5 % of a 32K window).
- There is no terse mode for the descriptions yet; `TOOL_GUARDIAN_PRESET=small` shortens only the
  catalogue.
