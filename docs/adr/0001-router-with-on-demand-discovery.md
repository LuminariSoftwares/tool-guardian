# 0001. A router with on-demand discovery

**Status:** accepted (0.1.0, still current)

## Context

An MCP client sends every connected server's tool definitions with every request, used or not. The bench
in `bench/` measures seven public servers at 20,005 tokens of schemas; on a 32K local model that is 61 % of
the window before the user types. The two usual answers are to connect fewer servers (lose the
capability) or to live with it (lose the window).

## Decision

Tool Guardian is itself one MCP server. It starts the user's servers as stdio children, reads their
`tools/list` once, and exposes only router tools to the client: `list_capabilities`,
`search_capabilities`, `describe_tool` and `call_tool` (plus `list_groups_with_costs`, `retrieve_spill`
and two skill tools when those features are on). A backend tool's schema reaches the model only when it
asks for it, and `call_tool(server, tool, args)` forwards the call unchanged. The config is the standard
`mcpServers` block, so an existing client config can be copied in (`tool-guardian-setup import`).

## Alternatives

- **Connect fewer servers.** Zero engineering, but the capability is gone.
- **Merge and rename the backend tools into one flat set.** Still pays for every schema on every request.
- **Client-side tool search.** Only where the client implements it; a stdio router works in any MCP client.
- **Curated, always-visible subsets.** Kept as the fallback: tool groups (`list_groups_with_costs`, and
  `activate_group` in the DSH bundle) price each group so the user can load exactly one.

## Consequences

- The fixed cost drops to the router tools (1,732 tokens on the seven-server bench, 91.3 % less).
- Every capability is now two or three calls away instead of one. The model has to *choose* to call
  `list_capabilities` first; the README's "Model requirement" section records one harness where small
  models did not, and one where the same model family did.
- The router is a single point of failure for every backend; see 0002.
- Only stdio backends are supported. An HTTP/SSE (`"url"`) entry is reported `UNSUPPORTED`.
