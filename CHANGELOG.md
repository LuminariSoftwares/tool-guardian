# Changelog

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
