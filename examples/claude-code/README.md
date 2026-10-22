# Claude Code

Two files, two different jobs. Mixing them up is the one mistake worth warning about.

## `project.mcp.json` — Claude Code's config, not the router's

Registers Tool Guardian with Claude Code. Either add it globally:

```bash
claude mcp add --scope user tool-guardian -- uvx tool-guardian
```

or copy this file to `.mcp.json` at the root of a project, where Claude Code asks once to approve a
project server.

**That `.mcp.json` is not Tool Guardian's own config.** The router reads
`~/.tool-guardian/mcp.json` (what `tool-guardian-setup import` / `add` write). A config in the
*current directory* — `./mcp.json` or `./.mcp.json` — is loaded only when you point the router at
one, with `--config ./mcp.json`, `TOOL_GUARDIAN_CONFIG=./mcp.json`, or
`TOOL_GUARDIAN_PROJECT_CONFIG=1`. A repository you cloned can carry one, and its servers are not
yours to start; when one is found and skipped you get a single stderr line saying so. A `.mcp.json`
whose only server is tool-guardian itself — exactly this file — never shadows the home config.

The startup log on stderr always names the config file and the `.env` that were actually loaded.

## `settings.json` — the PreToolUse hook

Bypass detection: it logs (or with `--mode deny` blocks) shell calls that do a router tool's job.
Goes in `~/.claude/settings.json` (all projects) or `.claude/settings.json` (one project). It never
starts a server and never fails a shell call — any problem means "allow". See
[bypass detection and its limits](../../README.md#bypass-detection-over-plain-mcp-and-its-limits).
