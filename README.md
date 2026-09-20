# Tool Guardian

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)

An MCP server that sits in front of your other MCP servers and exposes **three generic tools** instead of dozens of specific ones — discovering the rest **on demand** — so tool definitions stop eating your context window before the model reads a word.

Companion to [Context Guardian](https://github.com/LuminariSoftwares/context-guardian): **Context Guardian compacts the conversation before the window fills; Tool Guardian keeps the tools from filling it in the first place.** Two halves of the same problem.

## Why this exists

MCP tool definitions are re-sent on **every single request**, whether the model touches them or not. A handful of servers routinely comes to tens of thousands of tokens — often most of a small local model's window — before the first user message. On one real setup, seven MCP servers came to **28,689 tokens, 87.6% of a 32K window**, as a fixed floor under everything else.

You have two ways to deal with that today, and both cost you something:

| Approach | The cost |
|---|---|
| Load fewer MCP servers | You lose the capability entirely |
| Live with it | Two-thirds of the window is gone before you type |

Tool Guardian is a third option that costs neither. It fronts all your servers and shows the model just three tools plus a one-line catalogue of server names (~300 tokens). The full schema for a tool is fetched only when the model asks for it:

```
list_capabilities(server?)      one line per tool — names and purpose
describe_tool(server, tool)     the full argument schema for ONE tool
call_tool(server, tool, args)   invoke it, return the result
```

Same idea as a search index: cheap catalogue always visible, detail on demand.

## Model requirement (read this before you switch)

The whole design rests on one behaviour: the model must **proactively call `list_capabilities` (then `call_tool`) when it needs a tool.** Capable/frontier models do this reliably. **Smaller local models often do not** — faced with a task, they reach for their built-in tools (Bash/Read/shell) or a script and never open the catalogue, so the hidden tools are simply never reached.

This was measured directly (2026) against a real studio stack: **gpt-oss:20b and qwen3-30b-a3b both bypassed the router** on ordinary tasks — even with the `NEXT STEP` nudge in every result *and* a dedicated router sub-agent priming them. They either treated a tool name as a shell command or scripted their way around it. The token math worked perfectly; the models just wouldn't drive it.

So: `--selftest` proves the *saving* and that your backends start — it does **not** prove your model will use the router. **Test discovery→call with your actual model before committing.** If it won't reliably call these three tools, you're better off exposing a small *curated, visible* subset of servers than routing everything behind a catalogue the model never opens. The win here is real, but it's a win for models that ask.

## Where it sits

```
your CLI / agent (Claude Code, OpenClaude, any MCP client)
    -> Tool Guardian          (this project — one MCP server)
        -> your real MCP servers (filesystem, git, n8n, database, ...)
```

You point your client at **one** MCP server — Tool Guardian — and give Tool Guardian the same `mcpServers` config you'd have given the client. It starts your servers, keeps them warm, and proxies calls through on demand.

## Install

```bash
pip install tool-guardian
```

Pure standard library — nothing else to install.

## Configure

Tool Guardian reads the **standard** `mcpServers` block (the same shape Claude Desktop / Claude Code and most MCP clients use):

```json
{
  "mcpServers": {
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    },
    "git": {
      "command": "uvx",
      "args": ["mcp-server-git"],
      "description": "git status / diff / commit / log"
    }
  }
}
```

An optional per-server `"description"` enriches the catalogue the model sees. Without one, the hint is derived from that server's own tool names at startup.

Config is searched in order: `--config PATH`, `$TOOL_GUARDIAN_CONFIG`, `./mcp.json`, `./.mcp.json`, `~/.tool-guardian/mcp.json`.

## Environment and `.env`

Tool Guardian loads a `.env` itself and expands variables in your backend
args, so secrets don't have to be exported into the environment by whatever
launches it.

- **`.env` autoload.** On startup it looks for a `.env` in this order: an
  explicit path, `$TOOL_GUARDIAN_ENV`, then an **upward search** — starting at
  the config file's directory (or the cwd) and walking up to 5 parent
  directories, loading the first `.env` it finds. This lets your config live
  in a nested folder while the `.env` sits at the project root. Values already
  in the real environment win over the file; a missing `.env` is a no-op,
  never an error.

  Example — config nested under the project, `.env` at the root:

      myproject/
      ├── .env                 <- (3) found here, loaded, search stops
      └── config/
          └── dsh/
              └── mcp.json      <- $TOOL_GUARDIAN_CONFIG points here

  The search walks upward from the config's directory:

      1. myproject/config/dsh/.env    -> not found
      2. myproject/config/.env        -> not found
      3. myproject/.env               -> FOUND  (stops here)

- **Variable expansion.** `${VAR}`, `$VAR` and `%VAR%` are expanded in each
  backend's `args` from the environment; unknown variables are left as-is.
  Keep a secret in `.env` and reference it in a backend arg:

      "args": ["-y", "mcp-remote", "https://app.openseo.so/mcp",
               "--header", "Authorization: Bearer ${OPENSEO_API_KEY}"]

## Run

Point your MCP client at Tool Guardian as a single stdio server:

```json
{
  "mcpServers": {
    "tool-guardian": {
      "command": "tool-guardian",
      "args": ["--config", "/path/to/your/mcp.json"]
    }
  }
}
```

Everything your servers can do is still reachable — the model just discovers it in two steps (`list_capabilities` → `call_tool`) instead of paying for all of it up front.

## See what it saves

```bash
tool-guardian --selftest
```

Starts your configured servers, prints the catalogue, and reports the tokens the three router tools cost versus loading every server's tools directly — e.g. *"router tools cost ~310 tokens vs ~28,700 for the full set behind them → ~28,390 freed on every request."*

## It keeps tool *results* small too (0.3.0)

Definitions are half the problem; one 40 KB build log is the other half. Every `call_tool` result now goes down a deterministic **output ladder** before the model sees it:

| result | what the model gets |
|---|---|
| under ~1.2k chars, or from a `read`-class tool | untouched, byte for byte |
| an error over 300 chars | head + tail summary |
| JSON array / CSV ≥ 10k | keys, first and last items, counts |
| shell-style output ≥ 8k | head, evenly spaced samples (with line numbers), tail — `[exit code: …]` always kept |
| a unified diff | every change, plus the context right next to it |
| anything else ≥ 1.2k | cleaned losslessly: ANSI stripped, blank runs collapsed, repeated lines counted |

**Nothing is lost silently.** Before any lossy step the full original is archived, the result says so in one line, and the model can call **`retrieve_spill(id, grep=…)`** to read it back. If the archive cannot be written, the original is returned instead. Same input, same output, always — so provider prompt caches keep hitting. `TOOL_GUARDIAN_LADDER=0` turns it off.

`list_groups_with_costs` prices each tool group in context tokens, and every router call is logged (argument *values* never are) to `~/.tool-guardian/calls.jsonl` so you can measure whether your model actually uses the router.

## Native DeepSeek Harness (DSH) bundle

The same repo is an installable DSH bundle, `dsh-tool-guardian`. The Python router is unchanged — the bundle is a bridge to it, not a rewrite, and the MCP server above keeps working.

```sh
dsh plugin --profile <name> add dsh-tool-guardian        # or a path to a checkout (run `pnpm install` in it first)
dsh --profile <name> --dump-config                        # shows a "# == dsh-tool-guardian" layer
```

Inside DSH it (1) registers the router tools **natively**, so your MCP backends' schemas never enter a request unless you activate their group (`activeGroups`, or the `activate_group` tool, which quotes the token cost first); (2) runs the output ladder on **every** tool's result — `bash`, `grep`, `web_fetch`, all of them — through `tools/post-execute`, so do not mount `dsh-trim` beside it; (3) notices shell calls that do a router tool's job and logs, nudges (default) or denies them (`bypass.mode`). Configure it in the profile's `cordis.patch.yml` by overriding the `tool-guardian` row, or through the DSH settings namespace `tool-guardian`; the existing `TOOL_GUARDIAN_*` environment variables win over both. Python is found at `$TOOL_GUARDIAN_PYTHON`, then a `.venv` beside the package, then `python`/`python3` on `PATH` (3.9+, standard library only).

The result-shaping design follows [dsh-trim](https://www.npmjs.com/package/dsh-trim) (shuistama, MIT): `next()` first, fail open, archive before anything lossy.

## Design notes (the parts that matter)

- **Failure is loud, on purpose.** A router is a single point of failure: without one a broken server costs you that server; behind one it could cost you all of them. So an unreachable backend is reported as `UNKNOWN` with its real error, **never as an empty tool list**. A model that asks for a server and gets `[]` concludes the capability doesn't exist and quietly works around it — the exact failure this avoids.
- **Built for models, not just machines.** It accepts a tool's `args` as either an object or a JSON string, aliases the near-misses models actually send (`query`/`name` → `server`), and ends every result with the concrete **NEXT STEP** to call — because a model that receives a catalogue and no instruction tends to stop there instead of finishing the task.
- **The catalogue names your servers.** Three unnamed generic tools give a model no reason to believe any capability exists, so it improvises. Naming the servers in the tool description costs a few tokens and is the difference between a catalogue the model opens and three tools it ignores.

## What it does *not* do (yet)

- **stdio servers only.** An HTTP/SSE server (a `"url"` entry) is reported `UNSUPPORTED` — load it directly rather than through here.
- It does not merge or rename tools; it proxies them faithfully. `call_tool(server, tool, args)` reaches the real tool unchanged.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT — see [LICENSE](LICENSE).
