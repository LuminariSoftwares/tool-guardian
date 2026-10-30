<h1 align="center">Tool Guardian</h1>

<p align="center"><b>Seven MCP servers for 1,084 tokens instead of 20,005 &mdash; and tool results that stop flooding the window.</b></p>

<p align="center">One MCP server in front of all your others. Tested with <b>Claude Code, OpenCode, Cline and Continue</b> (one scripted-model turn per client, see <a href="#works-with">Works with</a>); Claude Desktop, Cursor and other stdio MCP clients: expected, not yet tested.</p>

<p align="center">
  <a href="https://github.com/LuminariSoftwares/tool-guardian/actions/workflows/test.yml"><img alt="tests" src="https://github.com/LuminariSoftwares/tool-guardian/actions/workflows/test.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/LuminariSoftwares/tool-guardian/actions/workflows/privacy.yml"><img alt="privacy" src="https://github.com/LuminariSoftwares/tool-guardian/actions/workflows/privacy.yml/badge.svg?branch=main"></a>
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-yellow.svg">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue.svg">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/python%20deps-none-2ea44f.svg">
  <img alt="MCP server" src="https://img.shields.io/badge/MCP-server-6f42c1.svg">
</p>

mcp-name: io.github.LuminariSoftwares/tool-guardian

The headline is this repo's bench, run against the recorded `tools/list` answers of seven public MCP servers (no network, no model; abridged where marked):

```text
$ python bench/bench_tokens.py
# Tool-Guardian Token Benchmark

Token counting method: estimate:chars/3.5

| server | tools | tokens (full schemas) |
...
| **Total** | **95** | **20005** |

**Every request:** 20005 tokens of tool schemas without tool-guardian, 1084 with it (94.6% less).
```

<p align="center">
  <a href="#quick-start-60-seconds">Quick start</a> &middot;
  <a href="#works-with">Works with</a> &middot;
  <a href="#deepseek-harness-results">DeepSeek Harness</a> &middot;
  <a href="#install-both-deepseek-harness-about-5-minutes">Install both</a> &middot;
  <a href="docs/first-5-minutes.md">First 5 minutes</a> &middot;
  <a href="#why-this-exists">Why</a> &middot;
  <a href="#two-ways-to-run-it">Two ways to run it</a> &middot;
  <a href="#architecture">Architecture</a> &middot;
  <a href="#install">Install</a> &middot;
  <a href="#see-what-it-saves">Measure it</a> &middot;
  <a href="#it-keeps-tool-results-small-too-030">Output ladder</a> &middot;
  <a href="#native-deepseek-harness-dsh-bundle">DSH bundle</a> &middot;
  <a href="#compatibility">Compatibility</a> &middot;
  <a href="CHANGELOG.md">Changelog</a>
</p>

An MCP server that sits in front of your other MCP servers and exposes **6 router tools** (8 with [skills](#skills)) instead of dozens of specific ones: four generic ones to list, search, describe and call the tools behind it, plus two for tool-group costs and archived results (see [Tools](#tools)) — discovering the rest **on demand** — so tool definitions stop eating your context window before the model reads a word.

The router never talks to the model backend, so it does not matter whether Ollama, LM Studio, vLLM or llama.cpp serves your model; what matters is that your client's model calls tools (run with Ollama; the others expected, not yet tested).

Companion to [Context Guardian](https://github.com/LuminariSoftwares/context-guardian): **Context Guardian compacts the conversation before the window fills; Tool Guardian keeps the tools from filling it in the first place.** Two halves of the same problem.

| | Without Tool Guardian | With it |
|---|---|---|
| 7 MCP servers on a 32K model ([bench](#measured-savings)) | **20,005 tokens** of schemas on every request (61 % of the window) | **1,084 tokens** (3 %); a schema is fetched only when the model asks |
| A 3,000-line build log with one error ([bench](#measured-savings)) | 115,945 characters land in the conversation | **4,540 characters**, the full original archived and one call away |
| A backend that fails to start | an empty tool list the model silently works around | status `UNKNOWN` plus the real error (e.g. `could not start: …`), never an empty list |

## Quick start (60 seconds)

Or tell your coding agent: `Install tool-guardian (docs: https://raw.githubusercontent.com/LuminariSoftwares/tool-guardian/main/llms.txt) and route my MCP servers through it.`

Nothing to install: [`uvx`](https://docs.astral.sh/uv/) runs it from PyPI. The example server below
is the MCP filesystem server, which runs with `npx`, so that one line needs Node.js — the router
itself is Python 3.9+ and standard library only.

```bash
uvx tool-guardian --version
uvx --from tool-guardian tool-guardian-setup add files -- npx -y @modelcontextprotocol/server-filesystem "$HOME"
uvx tool-guardian --selftest
claude mcp add --scope user tool-guardian -- uvx tool-guardian   # or any client: see "Run" below
```

Prefer it installed? `pip install tool-guardian`, then drop the `uvx` prefixes (`tool-guardian-setup add ...`,
`tool-guardian --selftest`).

Success looks like `added 'files' to ~/.tool-guardian/mcp.json`, then from `--selftest`
`1 backend(s) up, 14 tools reachable.` and a `router tools cost ~N tokens vs ~M` line. Already have
servers in Claude Desktop, Cursor or Windsurf? `tool-guardian-setup import` copies them instead of `add`.
In the client, ask for something one of your servers does; the model should call `list_capabilities`
(or `search_capabilities`) and then `call_tool`.

### What it looks like in practice

A real session against two servers (`files` and `git`), abridged only where marked. The model asks
for the catalogue, narrows to one server, checks one tool's arguments, then calls it:

```
> list_capabilities()
[files] 14 tools: read_file, read_text_file, read_media_file, read_multiple_files, write_file,
        edit_file, create_directory, list_directory, list_directory_with_sizes, directory_tree,
        move_file, search_files, get_file_info, list_allowed_directories
[git] 12 tools: git_status, git_diff_unstaged, git_diff_staged, git_diff, git_commit, git_add,
      git_reset, git_log, git_create_branch, git_checkout, git_show, git_branch

NEXT STEP: use search_capabilities with a keyword (or list_capabilities with one server name) to
see a tool's args, then call_tool.

> list_capabilities(server="files")
files.read_file: Read the complete contents of a file as text. DEPRECATED: Use read_text_file...
    args: path:string, ?tail:number, ?head:number
files.read_text_file: Read the complete contents of a file from the file system as...
    args: path:string, ?tail:number, ?head:number
... 11 more tools ...
files.list_allowed_directories: Returns the list of directories that this server is allowed to access....
    args: (no arguments)

NEXT STEP: you have not answered the user yet. Pick the tool above that does the job and use
call_tool with server files, that tool's name, and the args listed under it.

> describe_tool(server="files", tool="read_text_file")
files.read_text_file: Read the complete contents of a file from the file system as...
required args: path:string

NEXT STEP: use call_tool with server files, tool read_text_file, args: path:string, ?tail:number, ?head:number.

> call_tool(server="files", tool="read_text_file", args={"path": "project/README.txt"})
hello from tool-guardian

NEXT STEP: answer the user from this result.
```

Only the router's own six tools were in the model's context the whole time; the 26 backend tools
were never sent.

## When *not* to use the router

It is the wrong tool in two cases, and saying so is cheaper than finding out:

- **Your client already searches tools for you.** Some clients (and some model APIs) load tool
  definitions on demand themselves. Putting a router under that adds a hop and a second catalogue
  for no saving.
- **You run a handful of small servers.** The router's own tools cost about 1,100 tokens of
  `tools/list`; one small server behind it is close to break-even (`--selftest` prints the real
  arithmetic for *your* servers before you commit to it). The saving grows with every server added
  and is largest where it matters most — a 32K local model with half a dozen servers.

It also does not help if your model will not call tools reliably in the first place: see
[Model requirement](#model-requirement-read-this-before-you-switch).

## Works with

`pip install tool-guardian`, then one entry in your client. Every example below starts the same command, `tool-guardian`,
which reads `~/.tool-guardian/mcp.json`: `tool-guardian-setup import` fills it from the servers you already use.

| Client | Example | Where it goes | Tested |
|---|---|---|---|
| **Claude Code** | [`project.mcp.json`](examples/claude-code/project.mcp.json), hook: [`settings.json`](examples/claude-code/settings.json), [notes](examples/claude-code/README.md) | `claude mcp add --scope user tool-guardian -- uvx tool-guardian`, or copy to `.mcp.json` in a project (Claude Code asks once to approve a project server — that file is the client's config, not the router's) ; the hook goes in `~/.claude/settings.json` or `.claude/settings.json` | 2.1.296: full turn; hook in a live session |
| **OpenCode** | [`opencode.json`](examples/opencode/opencode.json) | `opencode.json` in the project, or `~/.config/opencode/opencode.json` | 1.18.35: full turn |
| **Cline** | [`cline_mcp_settings.json`](examples/cline/cline_mcp_settings.json) | VS Code: Cline &rarr; MCP Servers &rarr; Configure MCP Servers; CLI: `~/.cline/data/settings/cline_mcp_settings.json` | CLI 3.0.70: full turn |
| **Continue** | [`tool-guardian.yaml`](examples/continue/tool-guardian.yaml) | `.continue/mcpServers/` in the workspace (IDE), or its `mcpServers` entry in `config.yaml` | `cn` 1.5.47: full turn (entry in `config.yaml`) |

**What "full turn" means.** On Linux, 2026-10-10, each client got the example's `tool-guardian` entry unchanged
(Claude Code through `claude mcp add-json`; OpenCode and Continue with a test model provider added beside it; Cline the
file as is) and talked to a scripted model ([`tests/clients/mock_model.py`](tests/clients/mock_model.py), no real model, no API key). The client
sent tool-guardian's router tools to the model. The model's `list_capabilities` call went through tool-guardian to a stub
MCP server, and the answer came back to the model. tool-guardian's call log recorded the client by name (`claude-code`,
`opencode`, `@cline/core`, `continue-cli-client`). Re-run it: `pip install -e .` then `python tests/clients/run_clients.py`.
This proves the wiring, not that *your* model will use the router: see [Model requirement](#model-requirement-read-this-before-you-switch).

**The Claude Code hook in a live session.** The hook command from [`settings.json`](examples/claude-code/settings.json)
with `--mode deny` added (wrapped to record its input) blocked a Bash call naming a router tool inside a running Claude
Code 2.1.296 session. Claude Code
showed the hook's reason, `Use call_tool(server="probe", tool="tg_live_probe_tool", args={...}) instead of the shell`, and
the call log got its `bypass` row. The payload Claude Code sent is a test fixture now
([`tests/test_claude_code_hook.py`](tests/test_claude_code_hook.py)), next to the example from Claude Code's hook docs.

Not tested yet: the Cline VS Code extension (same file name and shape; tested through the Cline CLI), Continue's
`.continue/mcpServers/` folder (read by the IDE extension; `cn` reads only `config.yaml`), and client runs on Windows or
macOS (the unit tests run on all three in CI). On Windows, if a client cannot find `tool-guardian`, put the full path from
`where tool-guardian` in the `command`.

## DeepSeek Harness results

<p><img alt="DeepSeek Harness bundle" src="https://img.shields.io/badge/DSH-native%20bundle-0969da.svg"></p>

Tool Guardian also ships as a native [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) bundle, `dsh-tool-guardian` ([what it adds](#native-deepseek-harness-dsh-bundle)). These two figures come from DSH runs only and are not reproduced by anything in this repo:

- **Sessions that ended on a tool call written as text instead of made: 7/16 &rarr; 0/32** &mdash; DeepSeek Harness (version not recorded), qwen3-coder:30b, 8 tasks; 16 sessions (8 &times; 2) on dsh-tool-guardian 0.4.0-beta.2, 32 sessions (8 &times; 4) on 0.5.0-beta.1; the repair runs in the DSH bundle only; measured by the author, script and raw runs not yet published (see the 0.5.0-beta.1 entry of the [CHANGELOG](CHANGELOG.md)); not yet measured on other setups.
- **DSH first request:** 46 tools and 37,154 characters of schema without the bundle, 22 tools and 18,503 characters with it &mdash; one DSH setup, measured by the author, script not yet published.

<!-- shared "install both" block: keep identical to the Context Guardian README -->
## Install both (DeepSeek Harness, about 5 minutes)

Context Guardian and Tool Guardian are two halves of one problem: **Tool Guardian** keeps tool schemas and tool results from filling the window, and **Context Guardian** compacts the conversation before it fills and keeps what matters. They share no files and install separately. You need DSH 0.1.2-alpha.2 or later, Node.js `^22.19` or `>=24`, and Python 3.9+ on `PATH` for Tool Guardian's router.

```bash
# 1. Add both bundles to the DSH profile you use (`web` is the one `dsh web` uses)
dsh plugin --profile web add dsh-tool-guardian
dsh plugin --profile web add dsh-context-guardian

# 2. Tool Guardian: copy in the MCP servers you already use (Claude Desktop / Cursor / Windsurf / .mcp.json), then check them
cd ~/.dsh/profiles/web/node_modules/dsh-tool-guardian          # Windows: cd %USERPROFILE%\.dsh\profiles\web\node_modules\dsh-tool-guardian
npm run setup                  # asks before it writes ~/.tool-guardian/mcp.json, then prints a doctor report

# 3. Context Guardian: add its compaction row to your agent preset (a dry run until --apply)
cd ~/.dsh/profiles/web/node_modules/dsh-context-guardian       # Windows: cd %USERPROFILE%\.dsh\profiles\web\node_modules\dsh-context-guardian
npm run setup
npm run setup -- --apply

# 4. Start DSH and open a NEW session with the preset setup named
dsh web
```

In that session, type `/guardian`. It shows Context Guardian's engine revision and your model's window. Then type `/toolguardian`. It shows each MCP server Tool Guardian started, the tokens the router saves on every request, and whether an update is out.

Not on DSH? Context Guardian's proxy (`python context_guardian.py`, see its README) works with any OpenAI-compatible CLI. Tool Guardian runs as a plain MCP server for Claude Code, Cursor or any MCP client (see its README).

New to it? [Your first 5 minutes](docs/first-5-minutes.md) goes from nothing installed to proof that your model uses the router, on DSH or Claude Code.

## Why this exists

MCP tool definitions are re-sent on **every single request**, whether the model touches them or not. A handful of servers routinely comes to tens of thousands of tokens — often most of a small local model's window — before the first user message. On one real setup, seven MCP servers came to **28,689 tokens, 87.6% of a 32K window**, as a fixed floor under everything else.

You have two ways to deal with that today, and both cost you something:

| Approach | The cost |
|---|---|
| Load fewer MCP servers | You lose the capability entirely |
| Live with it | Two-thirds of the window is gone before you type |

Tool Guardian is a third option that costs neither. It fronts all your servers and shows the model just a handful of small tools plus a one-line catalogue of server names (1,084 tokens with seven servers behind it, measured below). The full schema for a tool is fetched only when the model asks for it:

```
list_capabilities(server?)      one line per tool — names and purpose
describe_tool(server, tool)     the full argument schema for ONE tool
call_tool(server, tool, args)   invoke it, return the result
```

Same idea as a search index: cheap catalogue always visible, detail on demand.

Every tool carries the MCP annotations (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). All of them are read-only except `call_tool`, which is marked destructive and open-world because it runs whatever the backend tool does; the DSH bundle's `tg__*` direct tools carry the same values.

## Model requirement (read this before you switch)

The whole design rests on one behaviour: the model must **proactively call `list_capabilities` (then `call_tool`) when it needs a tool.** Capable/frontier models do this reliably. Small local models are less certain — and, as the two measurements below show, **the harness the model runs in matters as much as the model.**

**Measurement 1 — August 2026, OpenClaude, router mounted over MCP.** Against a real multi-server stack, **gpt-oss:20b and qwen3-30b-a3b both bypassed the router** on ordinary tasks — even with the `NEXT STEP` nudge in every result *and* a dedicated router sub-agent priming them. They treated a tool name as a shell command or scripted their way around it. Part of that was the harness, not the models: gpt-oss:20b did call `list_capabilities` correctly but could not carry the discovery into `call_tool` inside a general task, and the one approach that completed the sequence (the sub-agent) was blocked by the harness's own agent-tool argument validation before it ever reached the router.

**Measurement 2 — 2026-09-19 and 2026-09-21, DeepSeek Harness, router tools registered natively by the [DSH bundle](#native-deepseek-harness-dsh-bundle).** The same model family (`qwen3:30b-a3b-instruct-2507`, 32K window) **used the router with no bypass.** 09-19, two sessions whose prompts named the tools: `list_capabilities` once, then `list_capabilities` ×8 → `call_tool(ops-scripts, service_status)`. 09-21, one session, three ordinary prompts that named no tool ("Which of the project's services are running right now?", "How many n8n workflows do we have, and which were edited most recently?", "What did we learn last time a bridge commit wrote stale bytes?"): the call log shows `list_capabilities(jobs)` → `call_tool(jobs, pipeline_status)`, then `call_tool(n8n, list)` (wrong name, `ok: false`) → `call_tool(n8n, n8n_list_workflows)` (`ok: true`, 12,963 chars shaped by the ladder) — the model corrected itself from the router's error — and **zero `bypass` rows**; the third prompt was answered with the companion plugin's `recall`/`search` plus built-in `glob`/`read`, which is the right tool, not a bypass. That is three sessions and one model: enough to show Measurement 1 is **not a verdict on these models**, not enough to promise yours will behave.

So: `--selftest` proves the *saving* and that your backends start — it does **not** prove your model will drive the router in your harness. **Test discovery→call with your actual model and harness before committing**, and measure rather than guess: every router call is appended to `~/.tool-guardian/calls.jsonl` (`TOOL_GUARDIAN_CALL_LOG`) with a session id, and under DSH a shell call that does a router tool's job is logged there as `kind: "bypass"` (and can be nudged or denied). After a few real tasks, **`/toolguardian bypass`** (DSH) or **`tool-guardian --bypass-summary`** (any client) reads that file for you: router calls, every bypass with the exact `call_tool(...)` it should have been, and sessions that never touched the router. Over plain MCP, see [bypass detection and its limits](#bypass-detection-over-plain-mcp-and-its-limits). If it won't, expose a small *curated, visible* subset of servers instead of routing everything behind a catalogue the model never opens.

## Two ways to run it

It is one repo and one Python router. Pick the front door that matches your harness — both stay supported.

| | **MCP server** (any MCP client) | **Native DSH bundle** |
|---|---|---|
| Works with | Claude Code, OpenCode, Cline, Continue ([tested configs](#works-with)), Claude Desktop, OpenClaude, Cursor, anything that speaks MCP over stdio | [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) |
| Install | `pip install tool-guardian` | `dsh plugin --profile <name> add dsh-tool-guardian` |
| Hides MCP schemas behind 6 router tools (8 with [skills](#skills)) | yes | yes, registered natively |
| Output ladder on results | results of `call_tool` | **every** tool's result (`bash`, `grep`, `web_fetch`, ...) |
| Tool groups with token prices | `list_groups_with_costs` | plus `activate_group`, and DSH's own built-in tools can be grouped and hidden too |
| Notices a shell call doing a router tool's job | — | logs, nudges or denies it |
| Configured by | `tool-guardian.json` + `TOOL_GUARDIAN_*` env | the `tool-guardian` patch row or DSH settings; the same env vars win |

```mermaid
flowchart LR
    A["Your agent<br/>(Claude Code, DSH, any MCP client)"] -->|"6 tools, ~1,700 tokens for 7 servers"| B["Tool Guardian"]
    B -->|"on demand"| C["filesystem"]
    B -->|"on demand"| D["git"]
    B -->|"on demand"| E["n8n, database, ..."]
    B -. "big result" .-> F[("archive<br/>retrieve_spill")]
    B -->|"shaped result"| A
```

## Tools

The router exposes these tools. The first four are always there; `list_groups_with_costs` and `retrieve_spill` (while the output ladder is on) are the other two of the 6 router tools; the two skill tools appear only when a skill folder is configured.

| tool | arguments | what it does |
|---|---|---|
| `list_capabilities` | `server` (optional) | tool names with one-line summaries for one server, or all; start here |
| `search_capabilities` | `query`, `server` and `limit` (optional) | find a tool by keyword across servers |
| `describe_tool` | `server`, `tool`, `detail` (optional) | the arguments of one tool (`detail=full`: its whole schema) |
| `call_tool` | `server`, `tool`, `args` (optional) | run a backend tool and return its (shaped) result; this is the step that answers the user |
| `list_groups_with_costs` | none | tool groups and the context-token cost of loading each |
| `retrieve_spill` | `id`, `grep`, `start_line`, `max_lines` (optional) | read back the full original of a shortened result |
| `list_skills` | none | one line per skill (only with `TOOL_GUARDIAN_SKILLS`) |
| `read_skill` | `name` | the full text of one skill (only with `TOOL_GUARDIAN_SKILLS`) |

All but `call_tool` only report (read-only, no network: the catalogue is in memory, the archive and skills on local disk); `call_tool` does whatever the backend tool does. Each tool's description says what it is for, when a sibling is the better call, what it touches and what comes back — in about one clause each, because the whole `tools/list` payload is capped at 1,100 tokens by a test. Every parameter is typed and explained exactly once, by its own description, by its `enum`, or by the tool description naming it; `tests/test_tool_definitions.py` checks that, and checks this table's argument names against the schemas the server really lists.

## Architecture

```mermaid
flowchart LR
    client["MCP client<br/>(Claude Code, Claude Desktop, ...)"] -- "stdio JSON-RPC" --> serve
    dsh["DSH bundle<br/>index.js"] -- "line JSON<br/>tg_bridge.py" --> router
    subgraph tg["tool-guardian (Python, standard library only)"]
        serve["serve()<br/>MCP over stdio"] --> router["Router<br/>catalogue · search · arg checks · loop guard"]
        router --> ladder["tg_ladder<br/>shape result by type"]
        ladder -- "lossy?" --> spill[("tg_spill archive<br/>~/.tool-guardian/spill")]
        router --> groups["tg_groups<br/>token price per group"]
        router --> state[("calls.jsonl · state.json")]
    end
    router -- "stdio, started at launch,<br/>respawned once" --> b1["backend MCP server"]
    router --> b2["backend MCP server"]
```

You point your client at **one** MCP server, Tool Guardian, and give it the same `mcpServers` config you would have
given the client. It starts every backend as a child process at launch, reads each `tools/list` once, and from then on
answers `list_capabilities`, `search_capabilities` and `describe_tool` from memory. Only `call_tool` reaches a backend.
Its arguments are checked against the backend tool's schema first, and the result goes through the output ladder before
the model sees it. The DSH bundle reaches the same `Router` through a private bridge process instead of MCP.

Key decisions, each with its trade-off (full records in [docs/adr/](docs/adr/)):

| Decision | What it buys | What it costs |
|---|---|---|
| Generic router tools, schemas on demand ([0001](docs/adr/0001-router-with-on-demand-discovery.md)) | 91 % fewer schema tokens on every request (bench) | every capability is 2–3 calls away; the model must choose to look |
| Loud failure, named catalogue ([0002](docs/adr/0002-loud-failure-and-a-named-catalogue.md)) | a dead server is never mistaken for "no such tool" | the description grows a few tokens per server |
| Descriptions budgeted and pinned ([0003](docs/adr/0003-descriptions-are-paid-on-every-request.md)) | tools a model can choose between without guessing | 1,090 tokens of `tools/list` with no backends, pinned at ≤ 1,100 by a test |
| Lenient inputs, strict backend checks ([0004](docs/adr/0004-lenient-inputs-strict-checks.md)) | small models' near-miss calls still work; impossible calls cost no round trip | schemas are looser than what the router enforces |
| One stdlib Python core; DSH bundle is a bridge ([0005](docs/adr/0005-one-python-core-stdlib-only.md)) | installs anywhere; one router to fix | MCP stdio is hand-written; the npm package needs a Python |
| Archive before shortening ([0006](docs/adr/0006-archive-before-shortening.md)) | big results stop flooding the window, nothing is lost | originals are kept on local disk |

## Install

```bash
pip install tool-guardian
```

Pure standard library — nothing else to install.

> PyPI and npm now ship the same code: `pip install tool-guardian` (0.3.0+) for the MCP server, `dsh-tool-guardian` on npm for DSH.

Which install is which: the Python package `tool-guardian` **0.8.1** is the MCP server (`pip install tool-guardian`, or `uvx tool-guardian`); the npm package `dsh-tool-guardian` **0.8.1-beta.1** is the DeepSeek Harness bundle. They are versioned separately.

## Configure

**Quickest way:** you probably already have your servers set up in another MCP client.

```bash
tool-guardian-setup import    # copies them from Claude Desktop / Cursor / Windsurf / .mcp.json, asks before writing
tool-guardian-setup doctor    # checks the config, each server's command, your .env and ${VARS}, and says how to fix each problem
```

**Adding one more server is one command** -- everything after `--` is the server's command line, exactly as you would type it:

```bash
tool-guardian-setup add git -- uvx mcp-server-git
tool-guardian-setup add github --env GITHUB_TOKEN='${GITHUB_TOKEN}' --description "issues and PRs" -- npx -y @modelcontextprotocol/server-github
tool-guardian-setup list      # name, command, and the group each server's tools land in
tool-guardian-setup remove git
```

`add` writes to the same config `doctor` finds (under DSH that is `$TOOL_GUARDIAN_CONFIG`), backs it up first, refuses a
name that already exists unless you pass `--replace`, checks the command is on your PATH, warns about any `${VAR}` that is
not set in your environment or `.env`, and ends with the doctor result for that one server. Then restart your MCP client
(or start a new DSH session) to load it.
Every server you add is automatically behind the router's 6 tools: with no groups config it gets its own group named
after the server; with a custom groups config it lands in `other` until you add it to a group.

`import` writes `~/.tool-guardian/mcp.json` (backing up any existing file), skips what it can't route yet
(HTTP/SSE servers) and prints the one entry to put in your client instead. Inside DSH (npm install) run the
same thing as `python <plugin folder>/tg_setup.py doctor`.

Or write the config by hand:

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

Config is searched in order: `--config PATH`, `$TOOL_GUARDIAN_CONFIG`, `~/.tool-guardian/mcp.json`. Whichever file is used is printed to stderr at startup, with the `.env` that was loaded beside it.

**A config in the current directory needs your say-so.** `./mcp.json` and `./.mcp.json` are *not* loaded automatically. A repository you cloned can carry one, and up to 0.7.0 starting the router inside that directory launched its servers — with no approval prompt from your client. When one is found and skipped you get a single stderr line naming it and the opt-in; to use it, pass `--config ./mcp.json`, set `TOOL_GUARDIAN_CONFIG=./mcp.json`, or set `TOOL_GUARDIAN_PROJECT_CONFIG=1`.

A `./.mcp.json` whose only server is tool-guardian itself (what the Claude Code setup below writes, to register the router with the client) is never treated as the router's own config: it is skipped and the search carries on to `~/.tool-guardian/mcp.json`.

## Environment and `.env`

Tool Guardian loads a `.env` itself and expands variables in your backend
args, so secrets don't have to be exported into the environment by whatever
launches it.

- **`.env` autoload.** On startup it looks for a `.env` in this order: an
  explicit path, `$TOOL_GUARDIAN_ENV`, then an **upward search** — starting at
  the config file's directory (or the cwd) and walking up to 5 parent
  directories, loading the first `.env` it finds. This lets your config live
  in a nested folder while the `.env` sits at the project root. Values already
  in the real environment win over the file, and the `TOOL_GUARDIAN_*` settings
  themselves can live there too; a missing `.env` is a no-op,
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

- **Small-window preset.** `TOOL_GUARDIAN_PRESET=small` is for 8k-context local
  models. It sets exactly two values:

  | setting | default | `small` | what it does |
  |---|--:|--:|---|
  | `TOOL_GUARDIAN_CATALOGUE_CHARS` | 0 (no cap) | 1500 | each server's block in `list_capabilities` is cut at this many characters, with a `[+N more tools not shown ...]` line pointing at `search_capabilities` |
  | `TOOL_GUARDIAN_DESC_WORDS` | 12 | 6 | words of a tool, server or skill description shown in catalogue and search lines |

  `small` also turns on `TOOL_GUARDIAN_TERSE` (below). Set either variable to override the preset. Search ranking still scores 12
  description words, the output ladder and every other setting are unchanged, and
  `describe_tool` is compact in both. On the seven bench catalogues the seven
  per-server listings total 9,582 characters with `small` against 16,274 without.

- **Terse router tools.** `TOOL_GUARDIAN_TERSE=1` serves the router's own tools with one-sentence
  descriptions (at most 160 characters), no per-parameter descriptions, and without
  `list_groups_with_costs`. Names, argument types, enums, defaults, required arguments and the MCP
  annotation hints are unchanged, and `list_capabilities` still names the live servers. `tools/list`
  drops from 1,090 to about 617 tokens. It is on under `TOOL_GUARDIAN_PRESET=small` unless you set
  `TOOL_GUARDIAN_TERSE=0`; unset or `0` leaves the default payload exactly as it was.

- **Variable expansion.** `${VAR}`, `$VAR` and `%VAR%` are expanded in each
  backend's `args` **and in its `env` values** from the environment; unknown
  variables are left as-is, so the backend's own error names the one you missed.
  Keep a secret in `.env` and reference it in a backend arg:

      "args": ["-y", "mcp-remote", "https://app.openseo.so/mcp",
               "--header", "Authorization: Bearer ${OPENSEO_API_KEY}"]

  or in a backend's environment:

      "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}

  (Up to 0.7.0 only `args` were expanded, so an `env` value like that reached
  the server as the literal text `${GITHUB_TOKEN}`.)

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

Claude Desktop (`claude_desktop_config.json`) without installing anything first, using [uv](https://docs.astral.sh/uv/):

```json
{
  "mcpServers": {
    "tool-guardian": {
      "command": "uvx",
      "args": ["tool-guardian", "--config", "/path/to/your/mcp.json"]
    }
  }
}
```

Claude Code, from a terminal (user scope, every project):

```bash
claude mcp add --scope user tool-guardian -- tool-guardian --config /path/to/your/mcp.json
```

In a container: `docker build -t tool-guardian .` then `docker run -i --rm -v "$HOME/.tool-guardian:/root/.tool-guardian" tool-guardian`.
The backends run inside the container too, so the image needs whatever they are started with (`npx`, `uvx`, ...); the
bundled [`Dockerfile`](Dockerfile) has Python only.

Everything your servers can do is still reachable — the model just discovers it in two steps (`list_capabilities` → `call_tool`) instead of paying for all of it up front.

### A backend that exits or stops answering

When a backend's process exits, or a call gets no reply within `TOOL_GUARDIAN_CALL_TIMEOUT`, the router stops it and
starts it again before the next call: after 1 s the first time, 5 s on any later respawn, re-sending the `initialize`
handshake. A second failure within 60 s marks the backend `DOWN` and it is not restarted again. The count is
`backend_respawns` in the bridge's `stats` and the `backend respawns: N since load` line of `/toolguardian`.

## See what it saves

```bash
tool-guardian --selftest
```

Starts your configured servers, prints the catalogue, and reports the tokens the router tools cost versus loading every server's tools directly — e.g. with one filesystem server behind it: *"router tools cost ~840 tokens vs ~3943 for the full set behind them -> ~3103 tokens freed on every request (79% smaller)"*. One small server is close to break-even; the saving grows with every server added. Below the totals it prints one `[server] N tools, ~M tokens` line per server, and it exits non-zero if any configured server failed to start. It ends with one `update:` line (a newer release, "none", or "could not check"); `tool-guardian-setup doctor` ends with the same line. The check runs at most once a day, is silent on failure, and is off with `GUARDIAN_NO_UPDATE_CHECK=1`.

Token counts use `tiktoken` (cl100k_base) when it is importable and the characters ÷ 3.5 estimate otherwise; the report prints which, e.g. `token counts: estimate (chars / 3.5; tiktoken not available)`. `pip install tiktoken` switches it; nothing else needs it.

### Prove your model drives the router

```bash
TOOL_GUARDIAN_PROVE_URL=http://127.0.0.1:11434/v1 tool-guardian --prove
```

Sends three bundled prompts to an OpenAI-compatible chat endpoint with the router tools attached, runs the model's tool calls through a real router in front of a bundled stub server, and prints one `PASS`/`FAIL` line per prompt with the prompt and completion tokens the endpoint reported: `list_describe` (list or search, then `describe_tool`), `nested_call` (`call_tool` with a nested filter), `stalled` (the model must reach `call_tool`, not only search). Exit 0 when all pass, 1 on any `FAIL`, 2 when `TOOL_GUARDIAN_PROVE_URL` is not set. Optional: `TOOL_GUARDIAN_PROVE_MODEL` (default: the first id from `GET <url>/models`), `TOOL_GUARDIAN_PROVE_KEY` (bearer token). The servers in your `mcp.json` are not started, and nothing is written to the call log or `state.json`.

Under DSH, type **`/toolguardian`** in a session for the same report from the running plugin: each server's status and tool count, the tokens freed on every request, active groups, ladder and bypass counters, and the update line.

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

## Skills

The same catalogue-on-demand idea, applied to `SKILL.md` files. Point `TOOL_GUARDIAN_SKILLS` at one or more folders (separated by `:` on Linux/macOS, `;` on Windows); every `SKILL.md` one or two levels below a root is a skill, named by its front-matter `name` (or its folder).

- **`list_skills`** — one line per skill: name and purpose. Its description names every skill, so the model knows they exist.
- **`read_skill(name)`** — the full text of one skill. A name is a name, never a path: `/`, `\` and `..` are refused.

The two tools appear only when at least one skill is found. `tool-guardian --skills-report` prints what the catalogue saves against loading every skill up front.

## Measured savings

Reproduce it yourself: `python bench/bench_tokens.py` (no network, no model; `--json` for machine output). The catalogs are the real `tools/list` answers of seven public MCP servers, recorded in `bench/fixtures/catalogs/`.

| server | tools | tokens of tool schemas |
|---|--:|--:|
| everything | 13 | 1,610 |
| filesystem | 14 | 2,500 |
| github | 26 | 5,094 |
| memory | 9 | 1,347 |
| n8n-mcp | 7 | 2,826 |
| playwright | 25 | 5,443 |
| sequential-thinking | 1 | 1,185 |
| **all seven** | **95** | **20,005** |

**Every request:** 20,005 tokens of tool schemas without tool-guardian, 1,084 with it (**94.6 % less**). On a 32K-context local model that is the difference between a window that is 61 % full before the conversation starts and one that is 3 % full. (The 2026-10-21 description rewrite had pushed this to 1,790; [ADR 0003](docs/adr/0003-descriptions-are-paid-on-every-request.md) records the budget that brought it back.)

The output ladder on four typical tool results:

| output | tool | before (chars) | after (chars) | how |
|---|---|--:|--:|---|
| 3,000-line build log with one error | bash | 115,945 | 4,540 | head, samples, the error line, tail, exit code |
| 400-item JSON array | http_get | 29,521 | 730 | keys, first and last items, counts |
| 3-hunk unified diff | git_diff | 41,764 | 1,363 | every change plus 2 lines of context |
| 1,500-row CSV | query | 44,882 | 354 | header, first and last rows, counts |

Every shortened result is archived first and can be read back with `retrieve_spill`. Token counts use `tiktoken` (cl100k) when it is installed and a conservative characters ÷ 3.5 estimate otherwise; the table above is the estimate.

## What it costs per task (measured 2026-10-01)

The saving above is **per request**. A tool behind the router has to be found before it can be called, and every extra
model request re-sends the conversation, so what matters for a whole task is *requests per task*. Measured on a real
DeepSeek Harness setup -- qwen3-coder:30b (Ollama, 64K window), 3 servers / 26 tools, 8 read-only tasks x 2 repeats, one
fresh session per task, the same tasks with the servers mounted directly vs. behind tool-guardian:

| | direct mount | tool-guardian 0.4.0 | tool-guardian 0.4.1 |
|---|--:|--:|--:|
| tools in the request header | 64 | 33 | 33 |
| header tokens per request | 17,831 | 10,825 | 10,825 |
| model requests per task | 1.9 | 4.8 | **2.8** |
| est. input tokens per task | 39,692 | 73,609 | **38,089** |
| wall time per task | 41 s | 75 s | 44 s |
| tasks passed (of 16) | 6 | 8 | 7 |

0.4.0 cut every request by 39 % but cost 85 % more per task: the model went search -> describe -> call, and copied the
call-shaped `NEXT STEP` hints as plain text. 0.4.1 puts each hit's arguments into the search result (no describe round
trip) and drops call syntax from the hints; per-task cost is now about even with a direct mount on 3 servers, and the
saving grows with every server you add. Break-even on this setup is ~3.7 requests per task. Pass rates at n = 16 are
inside noise. **When it pays off:** many servers, long sessions, a model that goes search -> call. **When it does not:**
one or two small servers with a model that explores. Reproduce: the harness and raw scores are in the 2026-10-01 entry of
the CHANGELOG.

## It catches the mistakes small models make (0.4.0)

Local models get tool calls *almost* right: a count sent as `"2"`, a required argument left out, a made-up option, the
same failing call repeated until the context runs out. The router now sits in front of those mistakes:

| mistake | what happens |
|---|---|
| a quoted number, `"true"`/`"false"`, a number where text is expected | fixed silently, then sent |
| a required argument missing, a wrong type, a value outside the allowed list, an unknown key on a closed schema | **not sent**; the model gets each problem and a correct example call |
| the same call returning the same result a 3rd time | the result is prefixed with a one-line "repeating this will not change the answer" |
| the same call a 5th time | not run; the model is told to use what it has or say what is blocking it |

Finding a tool is cheaper too: `search_capabilities(query)` returns the few matching `server.tool: description`
lines instead of a whole catalogue. Switches: `TG_VALIDATE_ARGS=0`, `TG_LOOP_GUARD=0`.

### Nested arguments

The checks above walk nested objects and array items too (required keys, types, enum values, closed objects), and a
problem names its full path: `argument 'params.filters[2].op' must be one of: eq, lt, gt`.

### `describe_tool` detail

`describe_tool(server, tool)` returns the compact form by default: `server.tool: one-line description`, a
`required args:` line, and the NEXT STEP line listing every argument (`?` = optional). `detail="full"` returns the
whole tool definition as before. On the seven bench catalogues (95 tools) compact is 25,161 characters against
131,080 for full, 80.8 % shorter. `TOOL_GUARDIAN_DESCRIBE_DETAIL=full` makes full the default.

## Native DeepSeek Harness (DSH) bundle

The same repo is an installable DSH bundle, `dsh-tool-guardian`. The Python router is unchanged — the bundle is a bridge to it, not a rewrite, and the MCP server above keeps working.

```sh
dsh plugin --profile <name> add dsh-tool-guardian        # or a path to a checkout (run `pnpm install` in it first)
dsh --profile <name> --dump-config                        # shows a "# == dsh-tool-guardian" layer
```

Inside DSH it (1) registers the router tools **natively**, so your MCP backends' schemas never enter a request unless you activate their group (`activeGroups`, or the `activate_group` tool, which quotes the token cost first); (2) runs the output ladder on **every** tool's result — `bash`, `grep`, `web_fetch`, all of them — through `tools/post-execute`, so do not mount `dsh-trim` beside it; (3) notices shell calls that do a router tool's job and logs, nudges (default) or denies them (`bypass.mode`). Configure it in the profile's `cordis.patch.yml` by overriding the `tool-guardian` row, or through the DSH settings namespace `tool-guardian`; the existing `TOOL_GUARDIAN_*` environment variables win over both. Python is found at `$TOOL_GUARDIAN_PYTHON`, then a `.venv` beside the package, then `python`/`python3` on `PATH` (3.9+, standard library only).

**Setup and checks.** `npm run setup` in the plugin folder finds Python, imports the MCP servers you already use into `~/.tool-guardian/mcp.json` (asking first) and runs the doctor; the bridge finds that file by itself, so no preset or YAML is edited. In a session, `/toolguardian` is the selftest, `/toolguardian bypass` (or `bypass last`, `bypass 24h`) the bypass summary, and `/toolguardian restore` re-loads the previous session's groups.

**Groups across sessions.** Each `activate_group` is remembered in `~/.tool-guardian/state.json` (`TOOL_GUARDIAN_STATE`; empty disables). The next session is *offered* those groups — a log line, a line in `list_groups_with_costs`, the `restore_groups` tool and `/toolguardian restore` — and never loads them on its own, because every loaded group costs tokens on every request.

**Settings.** There is no card for Tool Guardian in the DSH web UI (DSH draws cards only for plugins that ship a browser bundle); the settings live under `tool-guardian:` in `~/.dsh/settings.yaml` and apply live. [docs/dsh-settings.md](docs/dsh-settings.md) shows every field as a card and the exact precedence order, proven line by line from the code.

The result-shaping design follows [dsh-trim](https://www.npmjs.com/package/dsh-trim) (shuistama, MIT): `next()` first, fail open, archive before anything lossy.

## Bypass detection over plain MCP (and its limits)

Under DSH the plugin sees every shell call before it runs, so a bypass is logged, nudged or denied. **A plain MCP server cannot do that**: it only sees calls addressed to it, never the client's own `Bash`. What the server itself can see, and `tool-guardian --bypass-summary` reports:

- a session that **never called the router** (a `kind: "session"` row is written when a client connects);
- a session that **looked tools up but never called `call_tool`** — the pattern Measurement 1 recorded.

For real bypass detection in **Claude Code**, add its `PreToolUse` hook, which matches shell commands against the router's saved tool names and writes to the same call log (names only, never the command text; any error means "allow"):

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Bash|PowerShell", "hooks": [ { "type": "command", "command": "tool-guardian --hook-pretooluse" } ] } ] } }
```

`--mode deny` / `TOOL_GUARDIAN_HOOK_MODE=deny` blocks the call and tells Claude which `call_tool(...)` to use. The hook is tested against Claude Code's documented `PreToolUse` payload and against a payload captured from a live Claude Code 2.1.296 session, where deny mode blocked the call (see [Works with](#works-with)); [`examples/claude-code/settings.json`](examples/claude-code/settings.json) is the same block. Other clients without a pre-tool hook get the two server-side signals only.

### Hook rules file

```bash
tool-guardian --hook-pretooluse --rules ~/.tool-guardian/hook-rules.json
```

```json
{"deny":  [{"tool": "Bash", "args": {"command": "\\brm\\s+-rf\\b"}, "reason": "no recursive deletes"},
           {"tool": "WebFetch"}],
 "allow": [{"tool": "Bash", "args": {"command": "^git (status|log|diff)"}}]}
```

Rules run before the default behaviour. `tool` is a regex matched against the whole tool name; each `args` key is a
regex searched in that `tool_input` field; every listed key must match. Deny rules are checked first and win: the
call is blocked with `reason` (or a default naming the rule). An allow match skips tool-guardian's own check and lets
the call go on to Claude Code's normal permission flow; it approves nothing. No match, no file, or an unreadable file:
the default behaviour, unchanged. The hook only sees the tools its `matcher` sends it, so widen the matcher (e.g.
`Bash|PowerShell|WebFetch`) for rules on other tools.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| The model never calls the router — it answers from memory or reaches for the shell | small models bypass generic tools; this is the single most common failure | run a few real tasks, then `tool-guardian --bypass-summary`; if it keeps bypassing, that model will not drive a router — see [Model requirement](#model-requirement-read-this-before-you-switch) |
| Windows: the client reports `command not found` / the server never starts | `tool-guardian` is a script in a Scripts directory the client does not have on `PATH` | run `where tool-guardian` and put the full path in the client's `"command"`, or use `"command": "uvx", "args": ["tool-guardian", ...]` |
| A server shows as `UNKNOWN` or `DOWN` in `list_capabilities` | the backend failed to start or died | the row now carries the backend's **own last stderr lines** (`-- its stderr: ...`): read them first, they usually name a missing token or a bad path. `tool-guardian-setup doctor` prints a `fix:` line per problem. A `DOWN` backend is retried once more after 60 s |
| `config: ./mcp.json found but NOT loaded` on stderr | a config in the current directory is not loaded without consent (0.8.0) | if you meant to use it: `--config ./mcp.json`, or `TOOL_GUARDIAN_PROJECT_CONFIG=1`. If you did not, nothing is wrong — the file belongs to the directory you happen to be in |
| Zero backends although `~/.tool-guardian/mcp.json` has servers | some other config won the search | the startup log names the file that was loaded, and the `.env` beside it; `--config` or `$TOOL_GUARDIAN_CONFIG` overrides both |
| `retrieve_spill failed: ... it may have been pruned (keep=500)` | the archive keeps the newest `keep` results and that id aged out | re-run the tool with `call_tool`; raise `"spillKeep"` in the config's `toolGuardian` block if it happens often |
| A backend gets the literal text `${MY_TOKEN}` | you are on 0.7.0 or earlier, where only `args` were expanded | update to 0.8.0, where `env` values are expanded too (an unset variable still stays literal, on purpose) |

## Compatibility

| | Tested on | Expected to work | Notes |
|---|---|---|---|
| DeepSeek Harness (bundle) | 0.1.2-alpha.2, Windows 11 | later 0.1.x | peer `@deepseek-ai/dsh-tools >=0.1.2-alpha.2`, `@deepseek-ai/cordis ^4.0.2`; `/toolguardian` needs DSH's command registry (present in 0.1.2-alpha.2) |
| Node.js (bundle, `npm run setup`) | 22.x on Windows 11 and Linux | `^22.19.0` or `>=24` | the `engines` field |
| Python (router, bridge, setup) | 3.11 on Windows 11 and Linux; CI: 3.11 on Ubuntu, Windows, macOS and 3.9, 3.10, 3.12, 3.13 on Ubuntu | 3.9 – 3.13 | standard library only; every supported version is now run by CI |
| Operating system | Windows 11 (daily use); Ubuntu, Windows, macOS (CI: pytest and the node tests) | | the client runs in [Works with](#works-with) were on Linux only |
| Docker image | CI builds the [Dockerfile](Dockerfile) and speaks MCP to it over stdio on every push | | `python:3.12-slim`; the servers it fronts run in the container too, so a backend needing `npx` or `uvx` needs an image that has them |
| MCP servers behind the router | stdio | stdio | HTTP/SSE (`"url"`) servers are reported `UNSUPPORTED` |
| Clients (MCP server path) | Claude Code 2.1.296, OpenCode 1.18.35, Cline CLI 3.0.70, Continue `cn` 1.5.47 (one scripted-model turn each, Linux); OpenClaude | Claude Desktop, Cursor, the Cline and Continue IDE extensions, any stdio MCP client | see [Works with](#works-with); the Claude Code hook ran in a live session |
| Models | `qwen3:30b-a3b-instruct-2507` under DSH drove the router (3 sessions) | any model that calls tools | gpt-oss:20b and qwen3-30b-a3b bypassed it under OpenClaude — see [Model requirement](#model-requirement-read-this-before-you-switch) |
| Model backend | Ollama (via DSH / OpenClaude) | any | the router never talks to the model backend; only the model's tool-calling behaviour matters |
| Companion | dsh-context-guardian 0.1.0-alpha.5 | | the two share no files and install separately |

"Tested on" means this repo's test suites plus daily use on the machine it was built on. "Expected to work" is not tested — please open an issue if it does not work for you.

## Design notes (the parts that matter)

The longer versions, with alternatives and consequences, are the [decision records](docs/adr/).

- **Failure is loud, on purpose.** A router is a single point of failure: without one a broken server costs you that server; behind one it could cost you all of them. So an unreachable backend is reported as `UNKNOWN` with its real error, **never as an empty tool list**. A model that asks for a server and gets `[]` concludes the capability doesn't exist and quietly works around it — the exact failure this avoids.
- **Built for models, not just machines.** It accepts a tool's `args` as either an object or a JSON string, aliases the near-misses models actually send (`query`/`name` → `server`), and ends every result with the concrete **NEXT STEP** to call — because a model that receives a catalogue and no instruction tends to stop there instead of finishing the task.
- **The catalogue names your servers.** Six unnamed generic tools give a model no reason to believe any capability exists, so it improvises. Naming the servers in the tool description costs a few tokens and is the difference between a catalogue the model opens and six tools it ignores.
- **Hand-written protocol, no SDK.** The router speaks **MCP protocol revision `2024-11-05`** over stdio, both to its clients and to the servers behind it, implemented by hand in `tool_guardian.py` — no MCP SDK, no framework, nothing to install ([ADR 0005](docs/adr/0005-one-python-core-stdlib-only.md)). It is about 400 lines of JSON-RPC over two pipes, it is the reason `dependencies = []`, and it is why a single copy of the script still works on a machine with nothing but Python 3.9. Newer client revisions negotiate down to it; an unknown method gets a proper `-32601` so a client can fall back.
- **The token budget is a test, not an intention.** The router's own `tools/list` is re-sent on every request, so it is capped at 1,100 tokens by [`tests/test_tools_list_budget.py`](tests/test_tools_list_budget.py), measured end to end over stdio. Making a description nicer at the cost of the budget is the one change this project will not take ([ADR 0003](docs/adr/0003-descriptions-are-paid-on-every-request.md)).

<!-- guardian-family:start -->
## The guardian family

Four small MIT-licensed tools for people running local models. Each does one job, they share no code, and each is useful on its own.

| tool | what it answers |
|---|---|
| [code-guardian](https://github.com/LuminariSoftwares/code-guardian) | Before you run or merge code you did not write: is it safe, does it work, is anything missing? Three scores from a static scan, Python in depth. |
| [context-guardian](https://github.com/LuminariSoftwares/context-guardian) | Keeps a long local-model session inside its context window: a proxy that compacts history before the window fills, with tools to search what was archived. |
| [tool-guardian](https://github.com/LuminariSoftwares/tool-guardian) | Routes many MCP servers through a few tools, so their definitions stop filling the context window. |
| [stack-guardian](https://github.com/LuminariSoftwares/stack-guardian) | Is my home AI stack healthy? Ollama / OpenAI-compatible nodes, NVIDIA GPUs and Tailscale peers, with a confidence grade on every finding. |
<!-- guardian-family:end -->

## What it does *not* do (yet)

- **stdio servers only.** An HTTP/SSE server (a `"url"` entry) is reported `UNSUPPORTED` — load it directly rather than through here.
- It does not merge or rename tools; it proxies them faithfully. `call_tool(server, tool, args)` reaches the real tool unchanged.
- Over plain MCP it cannot see a client's shell calls; see [bypass detection and its limits](#bypass-detection-over-plain-mcp-and-its-limits).
- **The router's own tools cost about 1,100 tokens** of `tools/list` (with no backends; more with a long server list). With one small server behind it, that is close to break-even; it pays off from a few servers up. `TOOL_GUARDIAN_TERSE=1` (or `TOOL_GUARDIAN_PRESET=small`) brings it to ~620 tokens.
- The token figures are an estimate (characters ÷ 3.5) unless `tiktoken` is installed.
- It has no card in the DSH web UI; configure it in `~/.dsh/settings.yaml` ([docs/dsh-settings.md](docs/dsh-settings.md)).

## How it is tested

```bash
pip install -r requirements-dev.txt
python -m pytest -q                      # 212 tests; 3 skip until the npm install below has run
npm install --no-package-lock --legacy-peer-deps
npm run check && npm run test:node       # bundle syntax, text-call parser and repair
node tests/dsh_smoke.mjs && node tests/probe_dsh_mvp.mjs && node tests/probe_dsh_loopguard.mjs
node tests/probe_recall_line.mjs && node tests/probe_setup.mjs
pip install -e . && python tests/clients/run_clients.py   # real clients on PATH vs examples/ (optional)
```

- **Over the real protocol.** The tool-definition, annotation and README tests start `tool_guardian.py` as a stdio MCP
  server with a stub backend and read what `tools/list` actually returns.
- **Contract probes** (`tests/probes/`) were written from each module's contract before the module existed, so they
  check what a module's own selftest could not make true by being weak.
- **Regression tests fail first.** Each fix in the [CHANGELOG](CHANGELOG.md) names the test that failed on `main`
  before it.
- **Snapshots.** `tests/test_rank_identity.py` pins search and catalogue output on recorded catalogues;
  `bench/bench_tokens.py` re-measures every token figure in this README from `bench/fixtures/catalogs/`.
- **Clients.** `tests/clients/run_clients.py` drives Claude Code, OpenCode, Cline and Continue against a scripted model
  (no API key, no network); it is run by hand, not in CI.
- **CI** runs pytest on Ubuntu, Windows and macOS (Python 3.11) and Ubuntu (Python 3.9, 3.10, 3.12, 3.13), the node suites on all three
  operating systems, and a privacy scan of every tracked file.

## Acknowledgements

- **[dsh-trim](https://www.npmjs.com/package/dsh-trim)** (shuistama, MIT) — the shape of the result-shaping listener: call `next()` first, fail open, archive before anything lossy. Tool Guardian's ladder is an independent Python implementation; no dsh-trim code is included.
- **[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)** — the bundle format and the `tools/pre-execute` / `tools/post-execute` seams the DSH side is built on.
- The [Model Context Protocol](https://modelcontextprotocol.io) — the `mcpServers` config shape is theirs, used unchanged so your existing config works.

## License

MIT — see [LICENSE](LICENSE).
