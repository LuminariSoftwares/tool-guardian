# 0005. One standard-library Python core; the DSH bundle is a bridge

**Status:** accepted (Python core since 0.1.0; DSH bundle since 0.3.0)

## Context

The router has to run wherever the user's MCP servers run, including Windows machines with whatever
Python is on `PATH`, and it ships in two forms: an MCP server for any client, and a native DeepSeek
Harness (DSH) bundle that registers the router tools directly and shapes every DSH tool's result.

## Decision

- The router (`tool_guardian.py` and the `tg_*.py` modules) uses the Python standard library only:
  `dependencies = []` in `pyproject.toml`, Python 3.9+. `tiktoken` is used for token counts only when it
  is already importable.
- The DSH bundle (`index.js`) does not reimplement the router. It starts `modules/tg_bridge.py`, a
  private line-delimited JSON protocol over stdio, and registers the router tools with the schemas and
  descriptions the Python router returns. One codebase answers both front doors.

## Alternatives

- **A Node rewrite for DSH.** Native, but two routers to keep in step; every fix would land twice.
- **Use the MCP Python SDK.** Less protocol code, but a dependency tree for a tool whose pitch is that it
  installs anywhere with nothing else.

## Consequences

- The MCP stdio protocol is implemented by hand (`serve()`); only the methods a router needs are handled.
- The npm package ships the Python files (`files` in `package.json`) and needs a Python 3.9+ on the
  machine; `npm run setup` finds one and says how to fix it when there is none.
- Two version numbers (`tool-guardian` on PyPI, `dsh-tool-guardian` on npm) are released together but
  numbered separately.
