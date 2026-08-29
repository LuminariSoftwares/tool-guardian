# Changelog

## 0.1.0 — first release

An MCP server that fronts your other MCP servers behind three generic tools
(`list_capabilities`, `describe_tool`, `call_tool`) so their full definitions
stop being re-sent on every request.

- **Progressive disclosure:** the model sees ~300 tokens of router tools plus a
  one-line catalogue of server names; the full tool schemas are fetched only when
  it asks. `--selftest` reports the tokens freed vs loading every server directly.
- **Standard config:** reads the usual `mcpServers` JSON (Claude Desktop / Claude
  Code shape); searched via `--config`, `$TOOL_GUARDIAN_CONFIG`, `./mcp.json`,
  `./.mcp.json`, `~/.tool-guardian/mcp.json`. Optional per-server `description`
  enriches the catalogue; otherwise it's derived from the server's tool names.
- **Loud failures:** an unreachable backend is reported as `UNKNOWN` with its real
  error, never as an empty tool list — so a model can't conclude the capability
  doesn't exist and silently work around it.
- **Model-friendly:** accepts args as a JSON string or an object and aliases the
  common near-misses models send (`query`/`name` → `server`), and every result
  ends with the concrete NEXT STEP so the model calls the tool instead of stopping
  at the catalogue.
- stdio MCP servers only in this release; an HTTP/SSE (`url`) entry is reported
  `UNSUPPORTED`. Pure standard library — nothing to install.

Companion to [Context Guardian](https://pypi.org/project/context-guardian/):
Context Guardian compacts the conversation before the window fills; Tool Guardian
keeps the tools from filling it in the first place.
