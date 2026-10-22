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

## Amendment, 2026-10-22: the budget is a number and a test

The 2026-10-21 pass measured the wrong thing. `model_cost()` leaves out `title` and `annotations`
by design, so the 1,478 above was ~300 tokens short of the payload a client actually receives: the
real `tools/list` was **1,781 tokens**, against 986 for 0.7.0. At that size the router's own
definitions were a meaningful slice of the saving they exist to produce.

- The ceiling is now the real payload, measured end to end — start the server over stdio, complete
  the handshake, `tools/list`, `json.dumps(result["tools"])`, ÷ 3.5 — and it is **1,100 tokens**,
  enforced by `tests/test_tools_list_budget.py`, whose failure message prints the measurement and
  the per-tool breakdown. `tests/test_tool_definitions.py` pins the same number in-process.
- Each description still carries the four things above, but spends about one *clause* on each
  rather than a sentence, and a point the parameter list or an `enum` already makes is not repeated
  in prose. Parameters are explained exactly once: by their own description, by their `enum`, or by
  the tool description naming them. The per-parameter `examples` added on 2026-10-21 are gone —
  about 190 tokens, re-sent on every request, for information the description already carried.
- Two things that were genuinely missing were added, because a model that guesses wrong here pays
  more than the words cost: what `describe_tool`'s `server`/`tool` accept and what an unknown name
  returns, and what an unknown or pruned `retrieve_spill` id returns.
- The upper-case instruction in `list_capabilities` stays when there are servers to name (0002),
  but with nothing live the description now says so in sentence case: an upper-case block followed
  by "none reachable" is an unresolved template, not an instruction.

Measured after: **1,090 tokens** of `tools/list` with no backends, and **1,084** on the
seven-server bench (20,005 → 1,084, 94.6 % saved, 3.3 % of a 32K window). Raising `BUDGET` is a
decision; write the measurement in the CHANGELOG when you do.
