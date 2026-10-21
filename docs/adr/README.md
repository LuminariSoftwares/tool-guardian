# Architecture decision records

Short records of the choices the code already makes, written after the fact from the code, its comments
and the git history. Each one is context, decision, alternatives, consequences.

| # | Decision |
|---|---|
| [0001](0001-router-with-on-demand-discovery.md) | Front the MCP servers with a few generic router tools; fetch a tool's schema only when asked |
| [0002](0002-loud-failure-and-a-named-catalogue.md) | A dead backend is reported with its error, never as an empty list; the router's description names every server |
| [0003](0003-descriptions-are-paid-on-every-request.md) | Router tool descriptions are a per-request cost: budgeted, measured and pinned by a test |
| [0004](0004-lenient-inputs-strict-checks.md) | Accept what models send (aliases, JSON strings, clamped numbers); check backend arguments before sending |
| [0005](0005-one-python-core-stdlib-only.md) | One standard-library Python router; the DSH bundle is a bridge to it, not a rewrite |
| [0006](0006-archive-before-shortening.md) | Shorten large results only after archiving the original, and give the model a way back |
