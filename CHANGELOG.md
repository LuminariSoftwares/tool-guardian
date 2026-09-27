# Changelog

## Unreleased

**Find a tool by keyword.** A fourth router tool, `search_capabilities(query, server?, limit?)`, ranks tools across every
connected server by keyword (name hits beat description hits) and returns only the top few lines, in the same
`server.tool: description` format as `list_capabilities`. For a local model with a small window, discovery now costs a
handful of lines instead of a full catalogue.

**Bad arguments never reach the server.** Every `call_tool` is checked against the tool's own `inputSchema` first:
missing required arguments, wrong types, values outside an `enum`, and unknown keys when the schema is closed. A call
that cannot work is not sent; the model gets the exact problems and a corrected example instead of a server error it
has to decode. Safe fixes are applied silently: `"2"` becomes `2` for an integer, `"true"` becomes `true` for a boolean,
a number becomes a string where a string is expected. Turn it off with `TG_VALIDATE_ARGS=0`.

**Loops are named, then stopped.** The third identical call that returns the identical result is answered with a
one-line notice that repeating it will not change the answer; the fifth is not run at all. Calls whose results change
(polling a job, a counter) are never flagged. Turn it off with `TG_LOOP_GUARD=0`.

The router's stats now count `schema_rejects`, `schema_coercions`, `loop_warnings` and `loop_blocks`.

## dsh-tool-guardian 0.3.0-alpha.5 / PyPI tool-guardian 0.3.0 (2026-09-26)

**Know what it is doing without opening a log file.** Under DSH, type `/toolguardian` in a session. It is the
bundle's selftest: every MCP server with its status and tool count, the tokens the router frees on every request,
active groups, ladder and bypass counters, and one `update:` line. `/toolguardian bypass` (`bypass last`,
`bypass 24h`) summarises router calls against shell calls that did a router tool's job, with the exact
`call_tool(...)` for each. The CLI does the same for any client: `tool-guardian --bypass-summary [--session last|all|<id>] [--since-hours N]`.

**An update line where you look first.** `tool-guardian --selftest`, `tool-guardian-setup doctor` and
`/toolguardian` each end with one `update:` line: a newer release, "none", or "could not check". npm installs
check npm and share the plugin's once-a-day cache. pip installs check PyPI. The check is silent offline,
and `GUARDIAN_NO_UPDATE_CHECK=1` / `NO_UPDATE_NOTIFIER` / `CI` turn it off. New module: `tg_update.py`.

**Groups carry over, offered and never forced.** Each `activate_group` is recorded in `~/.tool-guardian/state.json`
(`TOOL_GUARDIAN_STATE`; empty disables). The next DSH session names those groups in its log and in
`list_groups_with_costs`, and loads them only when asked, through the new `restore_groups` tool or `/toolguardian restore`.
New module: `tg_state.py`.

**Bypass signals on the plain MCP path, with honest limits.** A plain MCP server cannot see a client's own
shell calls. Every call-log row now carries a session id, and a client connecting writes a `kind: "session"`
row, so the summary can name sessions that never called the router and sessions that looked tools up but
never called `call_tool`. For real detection in Claude Code there is an opt-in `PreToolUse` hook,
`tool-guardian --hook-pretooluse` (`--mode log|deny`). It matches shell commands against the tool names the
router saved at its last start. It logs names only, never the command text. It never blocks a shell call
because of its own error. It is tested against Claude Code's documented payloads, not yet in a live session.

**One setup command for DSH.** `npm run setup` in the plugin folder finds Python like the plugin does, runs
`tg_setup.py import`, then `doctor`, and prints the DSH next step. No preset or YAML edit is needed: the bundle's
patch already adds its row to the profile, and the bridge finds `~/.tool-guardian/mcp.json` by itself.

**Docs.** The README opens with the shared "Install both" block (Tool Guardian + Context Guardian). It now has
a compatibility matrix and a section on bypass detection over plain MCP.
[docs/first-5-minutes.md](docs/first-5-minutes.md) walks from nothing installed to a proven router call on
DSH or Claude Code. [docs/dsh-settings.md](docs/dsh-settings.md) shows the settings section as an ASCII card
(DSH draws no web card for a plugin without a browser bundle) and the precedence order proven from the code.
It covers `mcpServers` beating `TOOL_GUARDIAN_CONFIG`, settings.yaml merging into the patch row, and the
`toolGuardian` block in mcp.json sitting below DSH values. The README notes that PyPI still has 0.1.0.

Tests: contract probes `tests/probes/probe_tg_state.py` (19) and `probe_tg_update.py` (14), `tests/probe_dsh_mvp.mjs`
(21), `tests/probe_setup.mjs` (9) and `tests/test_mvp_wiring.py`. The test suite no longer touches the real
`~/.tool-guardian/state.json` or the network.

## dsh-tool-guardian 0.3.0-alpha.4 (2026-09-25)

**Setup you don't have to hand-write.** `tool-guardian-setup import` copies the MCP servers you
already configured in Claude Desktop, Cursor, Windsurf or a `.mcp.json` into tool-guardian's config
(asks first, backs up, skips HTTP/SSE servers and tool-guardian itself, prints the one client entry
to use). `tool-guardian-setup doctor` checks the config, every server's command on PATH, the `.env`
and unset `${VARS}`, and prints a fix line for each problem. npm/DSH users: `python tg_setup.py doctor`.

**Add or remove one server with one command.** `tool-guardian-setup add <name> -- <command> [args...]`
(`--env KEY=VALUE`, `--description`, `--config`, `--replace`) writes the server into the same config
`doctor` finds, backs the file up first, refuses duplicates unless `--replace`, keeps every other key
(your `toolGuardian` groups), checks the command is on PATH, warns about unset `${VARS}` in args or env,
and prints the doctor result for that server plus the reminder to restart your client.
`tool-guardian-setup remove <name>` backs up and removes one entry; `tool-guardian-setup list` shows
each server's command and the group its tools land in (its own name by default, `other` under a custom
groups config that does not name it). A config that is not valid JSON is refused, never overwritten.

### No tool is ever hidden by a custom group config

With a custom `groups` config, a tool that no selector covered used to vanish from
`list_groups_with_costs` (still callable, but invisible). It now lands in an automatic
`other` group (`ungrouped` if you already have a group named `other`), so the model always
sees every tool. The default (one group per server) is unchanged. If `other`, `ungrouped` and
`ungrouped_tools` are all taken by your own groups, the leftovers go to `other_2` (then `other_3`, ...)
instead of crashing.

## dsh-tool-guardian 0.3.0-alpha.3 — update notice (2026-09-25)

Once a day, in the background, the plugin asks the npm registry for a newer version and
prints one line if there is one (a prerelease user hears about newer prereleases and
stable releases; a stable user only about stable ones). Offline or failing checks are
silent. Turn it off with `GUARDIAN_NO_UPDATE_CHECK=1` (`NO_UPDATE_NOTIFIER` and `CI` are
honoured too).

## dsh-tool-guardian 0.3.0-alpha.2 — published to npm (2026-09-20)

Docs only: `dsh plugin --profile <name> add dsh-tool-guardian` now installs by name. No code change.

## 0.3.0 — output ladder, archive, group manifest, exact next steps; native DSH bundle

The router now also keeps tool **results** from filling the window, and ships as a
native DeepSeek Harness (DSH) bundle beside the unchanged MCP server.

- **Output ladder (`tg_ladder.py`)** — every `call_tool` result is shaped by type,
  deterministically: errors over 300 chars → head/tail summary; JSON arrays / CSV
  ≥10k → structure-aware (keys, first/last items, counts); shell-style output ≥8k →
  head + equidistant samples + tail, end-of-result markers (`[exit code: …]`) always
  kept; unified diffs → changes plus near context; everything ≥1.2k is first cleaned
  losslessly (ANSI, blank runs, repeated lines with a count). `read`-class tools are
  exempt. This replaces the old silent cut at 20,000 chars.
- **Nothing lossy without an archive (`tg_spill.py`)** — the full original is saved
  first and the result carries a one-line notice; the new **`retrieve_spill`** tool
  reads it back in windows or by regex. If the archive cannot be written, the
  original is returned instead.
- **Results are text, not envelopes** — `call_tool` returns the tool's text blocks
  rather than the MCP result as indented JSON (every newline escaped).
  `TOOL_GUARDIAN_RAW_RESULTS=1` restores the old shape.
- **`list_groups_with_costs` (`tg_groups.py`)** — tools arranged in groups (one per
  server by default, or `toolGuardian.groups` in the config), each priced in context
  tokens.
- **An exact NEXT STEP on every result** — `describe_tool` ends with the literal
  `call_tool(...)` line including required argument names; wrong server/tool names
  point at the exact recovery call; a shortened result names its `retrieve_spill` call.
- **Call log** — one JSON line per router call at `~/.tool-guardian/calls.jsonl`
  (`TOOL_GUARDIAN_CALL_LOG`, empty disables). Argument *values* are never written.
- **Native DSH bundle (`dsh-tool-guardian`)** — `package.json` + `cordis.patch.yml` +
  `index.js` + `modules/tg_bridge.py`. Router tools are registered natively; backend
  schemas never enter a request unless their group is active (`activeGroups`,
  `activate_group`); the ladder runs on **every** DSH tool's result via
  `tools/post-execute` (the role of the `dsh-trim` plugin, whose fail-open,
  spill-before-lossy design it follows — credit shuistama/dsh-trim, MIT); shell calls
  that do a router tool's job are logged / nudged / denied (`bypass.mode`).
- **Built-in tool groups (DSH bundle, `builtinGroups`, off by default)** — DSH's *own* tools can be
  grouped and hidden per agent until `activate_group(group=…)` asks for them, using the registry's
  per-agent `tools.restrict({ deny })`. On one measured profile four groups (subagents/workflow,
  goals, a plugin's 10 tools, web extras) were ~19,000 of a 37,000-char tool block. `activate_group`
  is registered from the first request so a hidden tool is always one call away;
  `list_groups_with_costs` lists the built-in groups with the exact call. `TOOL_GUARDIAN_BUILTIN_GROUPS=0` disables.
- New env: `TOOL_GUARDIAN_LADDER`, `TOOL_GUARDIAN_RAW_RESULTS`, `TOOL_GUARDIAN_CALL_LOG`,
  `TOOL_GUARDIAN_SPILL_DIR`, `TOOL_GUARDIAN_PYTHON` (bundle only).
- The three new modules are optional siblings: without them `tool_guardian.py` behaves
  as 0.2.0 did. No CLI flag changed.
- Fixed: `list_capabilities(server=…)` on a failed backend raised `AttributeError`
  instead of reporting the backend's real error.

## 0.2.0 — .env autoload + variable expansion

The router now loads its own `.env` and expands variables in backend args, so
secrets no longer have to be pre-exported into the environment by whatever
launches Tool Guardian.

- **`.env` autoload:** resolved by (1) an explicit path, (2) `$TOOL_GUARDIAN_ENV`,
  or (3) an **upward search** from the config directory (or cwd) up to 5 parent
  directories — so a nested config still finds a project-root `.env`. The real
  environment always wins over the file; a missing `.env` is a no-op.
- **Variable expansion in args:** `${VAR}`, `$VAR` and `%VAR%` in each backend's
  `args` are expanded from the environment (unknown vars left literal).
- New `tg_env.py` (standard library only); `tool_guardian.py` imports it and
  degrades gracefully if it is absent.

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
