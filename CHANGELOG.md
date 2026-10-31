# Changelog

## Unreleased

Per-tool safety policy: deny tools, ask before others (a confirm token), or make a session read-only.

- **`policy.json`.** `$TOOL_GUARDIAN_POLICY`, else `~/.tool-guardian/policy.json`, read when the router starts:
  `{"readOnly": bool, "servers": {"<server> or *": {"allow": [regex], "deny": [regex], "ask": [regex]}}}`. Regexes are
  `re.search`ed on the bare tool name; a server's entry and `*` merge. Order: `readOnly` (a tool whose annotations lack
  `readOnlyHint: true` is denied), `deny`, an `allow` list that matches nothing (denied, rule `allow`), `ask`. No file
  is today's behaviour exactly; a file that is not valid JSON fails closed (`POLICY_DENIED policy file unreadable: ...`).
- **Denied tools are invisible** in `list_capabilities`, `search_capabilities` and `list_groups_with_costs`;
  `call_tool` (or `describe_tool`) on one returns `POLICY_DENIED <rule>` and never reaches the server.
- **`ask` returns `CONFIRM_REQUIRED <token>`.** Call `call_tool` again with the same server, tool and args plus a
  top-level `confirm`. The token is HMAC-SHA256 under a per-process secret, single use, 120 s
  (`TOOL_GUARDIAN_CONFIRM_TTL`). `confirm` is not in the `call_tool` schema, so `tools/list` is unchanged. **The model
  confirms, not the user**; the human gate (elicitation) is a later release.
- **`tool-guardian --policy-check SERVER TOOL '<args-json>'`** prints `allow` / `deny <rule>` / `ask <rule>` and exits
  0 / 3 / 4; `--policy-check --list SERVER` prints one verdict per tool. Every deny and ask is a `kind: "policy"` row in
  the call log, and `--bypass-summary` adds `policy denials: <n>`. `--prove` runs with the policy off.
  New module `tg_policy.py`; `tests/test_p110_a3.py` is the acceptance file.

Remote MCP servers: a `"url"` entry is routed over Streamable HTTP instead of being reported `UNSUPPORTED`.

- **Streamable HTTP backends.** New module `tg_http.py` (standard library only). It tells the stateless 2026-07-28
  revision (`server/discover`) from the stateful 2025-11-25 / 2025-06-18 / 2025-03-26 ones (`initialize`, session id,
  `DELETE` on close) when the router starts, and a server that refuses `server/discover` for any reason other than a
  modern error is treated as legacy. `headers` take `${VAR}`; plain `http://` is limited to loopback unless
  `"allowInsecure": true`; `HTTPS_PROXY` and `NO_PROXY` are honoured. A `401` shows the server as `AUTH_REQUIRED`
  with its `WWW-Authenticate` header. `--selftest` prints `[remote] <name>: <era> <version> <n> tools`.
- Not done: OAuth, stream resumption and the `GET` stream, `subscriptions/listen`, elicitation, and HTTP+SSE
  (`"type": "sse"` stays `UNSUPPORTED`, "SSE lands in 0.9.1"). `import` still skips `url` servers.
  `tests/test_p110_a1.py` is the acceptance file and `tests/http_stub_server.py` the stub it runs against.

## dsh-tool-guardian 0.8.3-beta.1 / PyPI tool-guardian 0.8.3 (2026-10-30)

A counted usage report: `tool-guardian --usage` shows what the router saved, per server, from the call log.

- **`tool-guardian --usage [--since 7d|24h] [--json]`.** A counted report of what the router saved, read from the call
  log: per server the router calls, errors and result characters in and out, the catalogue tokens avoided
  (sessions x (the live servers' full `tools/list` cost - the router's own)), and the servers never called through
  `call_tool` with what dropping them would shrink the catalogue by. Counts only; the log holds no argument values, so
  the report has none. An empty window prints `no calls in window`; with `TOOL_GUARDIAN_CALL_LOG=""` it says the log is
  off, as `--bypass-summary` does. `--since` is an exact flag; `--bypass-summary --since` takes the same `7d` / `24h`
  (a bare number is hours, as the abbreviation of `--since-hours` always meant) and `--since-hours` is unchanged.
  New module `tg_usage.py`.
- **`state.json` records the catalogue's token cost.** At router startup the `catalogue` object also stores `tokens`
  (each live server's full `tools/list`, `est_tokens`) and `router_tokens` (the router's own `tools/list`); `servers`
  and `updated` are unchanged. `tests/test_p110_a4.py` is the acceptance file.

## dsh-tool-guardian 0.8.2-beta.1 / PyPI tool-guardian 0.8.2 (2026-10-30)

Terse mode for small models, `llms.txt` for coding agents, and the CI Python 3.9 job moved to `ubuntu-24.04`.

- **Terse mode.** `TOOL_GUARDIAN_TERSE=1` (also on under `TOOL_GUARDIAN_PRESET=small`; an explicit
  `TOOL_GUARDIAN_TERSE=0` turns it off even then) serves the router tools with one-sentence descriptions of at
  most 160 characters, no per-parameter descriptions, no repeated `title` inside `annotations`, and without
  `list_groups_with_costs`. Names, types, enums, defaults, `required` and the four annotation hints are unchanged,
  and `list_capabilities` still names the live servers (docs/adr/0002). Unset or `0` leaves the default payload
  byte-identical. `tests/test_tools_list_budget.py` gains `BUDGET_SMALL = 620`; `BUDGET = 1100` is unchanged.
  Measured `tools/list`, end to end as the existing test does: 1,090 tokens default, 617 terse (57 % of the default).
  The preset used to trim the catalogue but not the router's own six tools, so `small` still cost 1,090 tokens, about
  13 % of an 8k window before the first message. `tests/test_p110_a5.py` is the acceptance file.

- **`llms.txt` and a one-prompt agent install.** A short, link-only map for coding agents (the llmstxt.org
  convention): install, configure (including the rule that a config in the current directory is not loaded without
  the user's consent), the six router tools, and the limits. The README quick start now carries one line to paste to
  an agent that points it at the raw `llms.txt`. `llms.txt` ships in the npm package (`package.json` "files").
  `tests/test_p110_a6.py` checks the structure, that every link resolves, and the README line.

## dsh-tool-guardian 0.8.1-beta.1 / PyPI tool-guardian 0.8.1 (2026-10-24)

One hardening fix, plus docs, tests and CI.

- **Skills directory containment.** The skills scanner (`TOOL_GUARDIAN_SKILLS`) followed a symlinked skill
  directory, a symlinked group directory and a `SKILL.md` that was a symlink to a file outside the configured
  root, so `list_skills` advertised those skills and `read_skill` returned their text (the router's own
  `--skills-report` counted them). Every candidate is now resolved with `os.path.realpath` and skipped, with one
  stderr line naming it, unless it lies inside the resolved skills root; a Windows junction resolves the same way.
  A link that stays inside the root, and `..` in the configured root itself, still work.
  `tests/test_skills_containment.py` (6 tests failed on main, all pass now; the junction case runs on Windows
  only). `tools/list` unchanged at 1,090 tokens (budget 1,100).
- **README first screen** names the tested clients and shows `python bench/bench_tokens.py` with its output;
  `tests/test_glama_quality.py` checks the block against the bench. DSH-only figures moved to a "DeepSeek Harness
  results" section, each with its scope.
- **Guardian family block** shared byte-for-byte by the four guardian READMEs, pinned by `tests/test_family_block.py`.
- **Releases by trusted publishing (OIDC):** publishing a GitHub Release runs `.github/workflows/release.yml`,
  which checks that the tag names the `pyproject.toml` version, publishes each package only when its version is not
  on its registry yet (PyPI and npm are independent version lines; nothing new fails), runs the tests, builds, and
  publishes with short-lived OIDC credentials and signed provenance; no stored registry credential. Owner steps and
  the one-time registry setup: `docs/RELEASING.md`. `tests/test_release_workflow.py` fails if the workflow loses
  `id-token: write` or gains a password, token input or secret.
- **Support kit:** `SUPPORT.md` (one maintainer, best effort, acknowledgement within a day, no fix-time promise), issue forms for
  bug reports (version, OS, Python, exact command, full output, expected; required "I ran the quick start as written"),
  feature requests and questions, blank issues off, private security link. `tests/test_support_kit.py` fails if blank issues
  come back on, a form stops being valid, or `SUPPORT.md` disappears; PyYAML joins `requirements-dev.txt` (dev only).
- **CI actions on Node 24:** `actions/checkout` v7.0.1, `actions/setup-python` v7.1.0, `actions/setup-node` v7.1.0.

## dsh-tool-guardian 0.8.0-beta.1 / PyPI tool-guardian 0.8.0 (2026-10-22)

**External review fixes (2026-10-21 triage), each with a test that failed on `51e0a1e` first
(`tests/test_review_1021.py`, `tests/test_tools_list_budget.py`):**

- **Breaking: a config in the CURRENT DIRECTORY is no longer loaded implicitly.** `./mcp.json` and
  `./.mcp.json` used to be searched before `~/.tool-guardian/mcp.json`, so starting the router inside a
  repository you had cloned launched *that repository's* MCP servers, with no approval prompt from your
  client. They are now used only when you name one — **migration: set `TOOL_GUARDIAN_PROJECT_CONFIG=1`
  or pass `--config ./mcp.json`** (`TOOL_GUARDIAN_CONFIG` works too). A skipped one prints a single
  stderr line with its path and the opt-in. `--config` and `$TOOL_GUARDIAN_CONFIG` are unchanged.
- A cwd `.mcp.json` whose only server is tool-guardian itself — what the documented Claude Code step
  writes, to register the router *with the client* — no longer shadows `~/.tool-guardian/mcp.json`. That
  combination used to leave the router with zero backends. New `examples/claude-code/README.md` says
  which file is whose.
- The config file and the `.env` that were actually loaded are always logged to stderr at startup.
- **Router token budget.** The 2026-10-21 description pass measured `model_cost()`, which leaves out
  `title` and `annotations` by design and read 1,478 when the payload a client receives was **1,781
  tokens** of `tools/list` (0.7.0: 986). The descriptions return to the 0.7.0 shape — the four things
  each one must say, in about a clause each — plus only what was genuinely missing: what
  `describe_tool`'s `server`/`tool` accept and what an unknown name returns, and what an unknown or
  pruned `retrieve_spill` id returns. Per-parameter `examples` are gone (~190 tokens on every request
  for what the descriptions already said), and a parameter is explained exactly once: by its own
  description, its `enum`, or the tool description naming it. **Measured: `tools/list` 1,781 → 1,090
  tokens; the seven-server bench 1,790 → 1,084 (20,005 → 1,084, 94.6 % less).** `tools/list` is now
  capped at 1,100 by `tests/test_tools_list_budget.py`, which starts the real server over stdio;
  `tests/test_tool_definitions.py` pins the same number in-process (it used to pin the understated
  `model_cost()` at 1,500). Tool names, arguments, annotations and behaviour are unchanged.
  [ADR 0003](docs/adr/0003-descriptions-are-paid-on-every-request.md) has the amendment.
- **Fix: `${VAR}` is expanded in a backend's `env` values**, not only its `args` — the README's own
  `tool-guardian-setup add github --env GITHUB_TOKEN='${GITHUB_TOKEN}'` passed the literal text. An
  unset variable still stays literal, as for `args`, so the backend's error names it.
- **Fix: a crashed backend keeps its own error.** Backends ran with `stderr=DEVNULL`, so a server that
  printed `FATAL: GITHUB_TOKEN is not set` and exited reached the user as `BrokenPipeError`. Its last
  20 stderr lines are kept in a ring buffer and appended to the `UNKNOWN`/`DOWN` error text, so they
  show up in `list_capabilities`, `search_capabilities`, `call_tool` and `--selftest`.
- **Fix: an unknown JSON-RPC method answers `-32601`** instead of `{"result": {}}`, so a client probing
  the stateless 2026-07-28 revision's `server/discover` gets "method not found" and falls back.
  `ping` keeps its empty result, now because the spec says so rather than because the old catch-all
  answered everything that way.
- **Fix: a JSON array, string or number on stdin answers `-32600` and the session survives.** Such a
  line used to raise `AttributeError: 'list' object has no attribute 'get'` and end the router.
- `sys.stdin` is reconfigured to UTF-8 in both `serve()` and the hook: MCP stdio is UTF-8 whatever the
  Windows console code page is, and non-ASCII arguments arrived as mojibake.
- **Fix: a `DOWN` backend can come back.** Two bad calls within 60 s marked it `DOWN` for the whole
  session, however long that session ran. Once the window has passed it gets one more respawn attempt
  with backoff; the status names and the error text are unchanged.
- The spill directory is `0700` and spill files are `0600`; `calls.jsonl` is `0600` (`mcp.json` and
  `state.json` already were). No-op on Windows, where POSIX modes do not apply.
- `retrieve_spill` on an unknown id keeps its `retrieve_spill failed:` prefix and adds that it may have
  been pruned, with the archive's `keep` value; the `keep` setting is documented for plain MCP users.
- **Docs.** Quick start leads with `uvx tool-guardian` (nothing installed; the filesystem example still
  needs Node). New: a real `list_capabilities` → `describe_tool` → `call_tool` transcript, a "When *not*
  to use the router" section, a troubleshooting table, a guardian-family paragraph linking the other
  three, and the protocol stated plainly (MCP `2024-11-05`, hand-written, no SDK).
  `docs/first-5-minutes.md` no longer tells you PyPI is stuck on 0.1.0. `SECURITY.md` gains "backends
  are trusted code" and "what leaves the machine" (one update check a day, off with
  `GUARDIAN_NO_UPDATE_CHECK=1`, `NO_UPDATE_NOTIFIER=1` or `CI`).
- **CI.** Adds Python 3.10, 3.12 and 3.13 on Ubuntu (every supported version is now run) and a job that
  builds the Dockerfile and speaks MCP to the image over stdio. Third-party actions are pinned to
  commit SHAs. Existing job names are unchanged.
- PyPI summary said "four small generic tools"; there are six.

- `Dockerfile` runs as a non-root user `guardian` (uid 10001); mount config at `/home/guardian/.tool-guardian`.
- Fix: `tool-guardian-setup` recognises a scoped npm package (`npx -y @scope/tool-guardian`,
  `@scope/dsh-tool-guardian@0.6.0`) as tool-guardian itself; folder paths still do not match.

**Glama and portfolio pass (2026-10-21):**

- **Tool definitions.** Every router tool's description says what it is for, when a sibling tool is the better call,
  what it touches (read-only in memory or on local disk; `call_tool` has the backend tool's side effects) and what
  comes back. Every parameter has a type, a description, examples, and a default or what omitting it does;
  `describe_tool.detail` gains `type` and the default the router really uses. No `minimum`/`maximum`/`pattern`:
  the router clamps those values itself ([ADR 0004](docs/adr/0004-lenient-inputs-strict-checks.md)).
  `tests/test_tool_definitions.py` enforces all of it and checks the README tool table against the live schemas.
- With no live backend, `list_capabilities` no longer reads "THESE SERVERS ARE AVAILABLE ...: none reachable"
  (Glama's 2026-10-15 review read it as an unresolved template); it says no server is reachable yet and that the
  call returns why. `call_tool` says how failures come back; `list_groups_with_costs` names `activate_group`.
- **Cost of that, stated:** the router's tools with no backends go from 942 to 1,478 tokens (estimate), pinned at
  1,500 by a test; the seven-server bench from 1,209 to 1,790 tokens (94.0 % → 91.1 % less). README figures
  re-measured with `bench/bench_tokens.py`. Tool names and behaviour are unchanged.
- **Fix: `tool-guardian-setup add`/`import` refused a server whose path merely contained "tool-guardian"**
  (for example a filesystem server rooted in a clone of this repo) as "tool-guardian itself". The command is now
  matched by file name and an argument only as the package, module or script. `tests/test_setup_is_self.py`.
- `docs/adr/`: six decision records derived from the code and its history.
- README: a 60-second quick start (verified from a clean virtualenv), an Architecture section with a diagram and
  the decisions' trade-offs, a Claude Code command, how it is tested, and the router's own cost among the limits.
- `Dockerfile` (Python only; `docker run -i`) and `.dockerignore`, for container users and directory build checks.
- Module docstring no longer says three tools and ~300 tokens.
- Already on `main` since 0.7.0 without an entry: SPDX licence metadata (`license = "MIT"`, `license-files`,
  setuptools ≥ 77), `glama.json`, the README Tools table and Claude Desktop `uvx` example.

## dsh-tool-guardian 0.7.0-beta.1 / PyPI tool-guardian 0.7.0 (2026-10-14)

- MCP Registry name uses the GitHub owner's case: `io.github.LuminariSoftwares/...` (the registry rejects the lower-case form).

- `server.json` for the official MCP Registry (`uvx tool-guardian`).

**MCP tool annotations and Registry markers (2026-10-15):**

- **MCP tool annotations.** Every tool the router and the DSH bundle expose has a `title` and `readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`. The discovery, archive and skill tools are read-only;
  `call_tool` and the bundle's `tg__*` direct tools are destructive and open-world (they run the backend's tool);
  `activate_group` / `restore_groups` change the session's tool set but delete nothing. The token figures in
  `--selftest` and `list_groups_with_costs` leave the annotations out: they are client metadata (UI, permission prompts), not schema text.
  Tests: `tests/test_tool_annotations.py` (tools listed over MCP), `tests/dsh_smoke.mjs`.
- **MCP Registry markers** for the next release: `"mcpName": "io.github.luminarisoftwares/tool-guardian"` in
  `package.json` and the matching `mcp-name:` line in README.md (the README PyPI and npm ship). No `server.json` yet.

**The "Later" list from the 2026-10-10 review (2026-10-14)**, each with a test that failed on main before it
(`tests/test_later_1014.py`, `tests/probe_recall_line.mjs`):

- Argument checks walk nested objects and array items; a problem names its path (`params.filters[2].op`).
- A backend that exits or times out is respawned once (1 s, then 5 s on later respawns) with a fresh `initialize`
  handshake; a second failure within 60 s marks it `DOWN`. `backend_respawns` is in `stats` and on `/toolguardian`.
- `describe_tool` takes `detail=compact|full`; compact (name, one line, required args) is the default and is 80.8 %
  shorter than full on the bench catalogues. `TOOL_GUARDIAN_DESCRIBE_DETAIL=full` restores the old default. The
  router's own schemas grow from 934 to 951 tokens on the bench (the new `detail` property).
- `tool-guardian --prove` runs three bundled prompts against `TOOL_GUARDIAN_PROVE_URL` and prints PASS/FAIL with tokens.
- `--hook-pretooluse --rules <file>`: JSON allow/deny rules per tool name and argument regex, applied first.
- `--selftest` counts tokens with `tiktoken` when it is importable and says which counter it used.
- `TOOL_GUARDIAN_PRESET=small`: per-server catalogue budget 1500 characters, 6-word descriptions.
- `tests/test_rank_identity.py` asserts search, catalogue and score output on the rank fixtures is unchanged.

## dsh-tool-guardian 0.6.0-beta.1 / PyPI tool-guardian 0.6.0 (2026-10-10)

**Fixes from the 2026-10-10 review**, each with a test that failed before it (`tests/test_review_1010.py`,
`tests/probe_recall_line.mjs`):

- `TOOL_GUARDIAN_START_TIMEOUT` and `TOOL_GUARDIAN_CALL_TIMEOUT` are enforced. A backend that never answers is
  stopped and reported `UNKNOWN` at the start timeout instead of holding start open; a call that times out returns a
  tool error and frees the backend for the next call.
- `--bypass-summary`: only `call_tool` counts as invoking a tool, so a session that only searched is reported as
  stalled; `search_capabilities`, `list_groups*`, `list_skills`/`read_skill` and `retrieve_spill` count as discovery.
- Spill archive: the sidecar is written before the text, a missing or unreadable sidecar is rebuilt instead of
  failing every later save, and pruning survives a file deleted under it.
- Archive failures are counted (`archive_failures`) and shown on the `/toolguardian` output-ladder line.
- `call_tool` without `server` accepts `server.tool` or a tool name only one server has; an ambiguous name gets an
  error listing the candidates.
- `retrieve_spill` past the end says `no lines at start_line N; total_lines M`.
- `TOOL_GUARDIAN_*` settings in `.env` are honoured.
- `--selftest` prints `[server] N tools, ~M tokens` per server and exits non-zero when any backend failed; the
  catalogue shows 160 characters of a backend error instead of 80.

**Privacy CI.** `.github/workflows/privacy.yml` fails a push or pull request when a tracked file holds a tailnet
address or name, a personal Windows path, an API key or an unexpected e-mail address; its log names only file, line
and pattern number.

**Works with Claude Code, OpenCode, Cline and Continue.** `examples/` has one config per client, each starting plain
`tool-guardian`. Each was run on Linux by the real client (Claude Code 2.1.296, OpenCode 1.18.35, Cline CLI 3.0.70,
Continue `cn` 1.5.47) against a scripted model: router tools reached the model, a `list_capabilities` call went through
to a stub server and back. `tests/clients/run_clients.py` repeats it with whatever clients are on PATH.

**The Claude Code hook ran in a live session.** In deny mode it blocked a Bash call naming a router tool inside Claude
Code 2.1.296. The payload Claude Code sent and the example from its hook docs are both test fixtures now
(`tests/test_claude_code_hook.py`). `examples/claude-code/settings.json` matches `Bash|PowerShell`.

**CI on three operating systems.** pytest and the node tests (`check`, `test:node`, the DSH smoke and probes) run on
Ubuntu, Windows and macOS. No change to the router, the ladder or any runtime code.

## dsh-tool-guardian 0.5.0-beta.1 / PyPI tool-guardian 0.5.0 (2026-10-04)

**Tool calls written as text are repaired (DSH engine).** Local models sometimes write a tool call into the reply text
instead of making it, and the session ends on a call that never ran. The plugin now spots those text calls in the
model's stream and turns them into real calls. Benchmark (atlas qwen3-coder:30b, 8 tasks x 4 runs):

| measure | 0.4.0-beta.2 (16 sessions) | this release (32 sessions) |
|---|---|---|
| sessions ending on a text-only tool call | 7/16 | 0/32 |
| sessions where a text call was repaired into a real call | -- | 12/32 |
| tasks passed | 7/16 | 22/32 |

`textToolCallRepair: { enabled: false }` or `TOOL_GUARDIAN_TEXTCALL_REPAIR=0` turns it off; `logOnly: true` only logs.

- **Empty results say so.** When a backend tool returns nothing, the router now tells the model the result was empty
  and to try another route, instead of "answer the user from this result" (the model used to answer "not found").
  `TG_EMPTY_NUDGE=0` turns it off.
- **`server.tool` names work in `call_tool`.** `list_capabilities` prints `server.tool`; calling that exact name used
  to fail with "has no tool".
- **Discovery ceiling.** A model that keeps listing or searching tools without calling one is stopped after
  `discoveryLimit` (default 8) discovery calls in a row; any real call resets the count.
- **Per-server search hints (opt-in).** A backend may carry a `hints` word list that boosts its tools in
  `search_capabilities`. Not set by default: an A/B on two code-search tasks showed no gain (3/16 with and without).
- `tg_rank.py` and `tg_ceiling.py` now ship in both the npm package and the wheel.

## dsh-tool-guardian 0.4.0-beta.2 / PyPI tool-guardian 0.4.1 (2026-10-01)

**One-step discovery.** `search_capabilities` now shows `args: name:type, ?optional:type` under its top three hits and
`list_capabilities(server)` under every tool, so the model can call straight from a search result; `describe_tool`
becomes the exception. A `call_tool` rejected for its arguments now lists the tool's arguments, so the retry needs no
describe either.

**No call-shaped text in results.** `NEXT STEP` lines used to end with an example like `call_tool(server="x", ...)`;
small local models copied that as plain text instead of making a tool call. They now name the server, tool and
arguments in words.

**Measured.** 32 DeepSeek Harness sessions (qwen3-coder:30b, 3 servers / 26 tools, 8 tasks x 2): model requests per task
4.8 -> 2.8, estimated input tokens per task 73,609 -> 38,089 (direct mount: 39,692), wall time 75 s -> 44 s (direct:
41 s). See the README section *What it costs per task*.

## dsh-tool-guardian 0.4.0-beta.1 / PyPI tool-guardian 0.4.0 (2026-09-27)

**A benchmark anyone can re-run.** `bench/bench_tokens.py` measures the schema tokens of seven public MCP servers
(95 tools, recorded in `bench/fixtures/catalogs/`) against the router's own tools, and runs the output ladder on four
typical tool results. Result: 20,005 tokens per request without tool-guardian, 934 with it (95.3 % less). The README's
new *Measured savings* section is its output.

**Recall tracking.** The router counts, per tool, how many shortened results were archived and how many the model read
back with `retrieve_spill`; `/toolguardian` shows `recall after shortening: bash 2 of 14 (14%), ...`. A tool the model
keeps recalling is being shortened too hard.

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

**The loop guard covers DSH's own tools too.** Under the DSH bundle the same 3rd-warn / 5th-refuse rule now applies to
the harness's built-in tools (bash, pwsh, grep, web_fetch, ...), which never pass through the router. Same messages,
same thresholds (`loopGuard.warnAt` / `loopGuard.blockAt`, `TG_LOOP_GUARD=0` turns it off). `/toolguardian` gains a
`call checks:` line with bad calls stopped, calls fixed, repeat warnings and repeats refused.

**Python 3.9 actually works.** The package has always said `>=3.9`, but `tg_spill.py`, `tg_state.py` and `tg_env.py` used
`str | None` annotations, which crash on import under 3.9. They now use postponed annotations; the test suite passes on
3.9 and 3.11, and CI runs both. `pytest` (not only `python -m pytest`) now finds the package from a fresh clone.

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
