# Your first 5 minutes with Tool Guardian

This page goes from nothing installed to proof that your model uses the router. There are two paths: DeepSeek Harness (DSH), and any other MCP client (Claude Code is the example).

## Path A: DeepSeek Harness

You need DSH 0.1.2-alpha.2 or later, Node.js `^22.19` or `>=24`, and Python 3.9+ on `PATH`. The examples use the profile `web`, the one `dsh web` starts.

**1. Install the bundle (about 30 s).**

```bash
dsh plugin --profile web add dsh-tool-guardian
```

The bundle adds its own row to the profile. You do not edit any preset or YAML. Check it is there:

```bash
dsh --profile web --dump-config      # shows a "# == dsh-tool-guardian" layer
```

**2. Give it your MCP servers (about 1 min).**

```bash
cd ~/.dsh/profiles/web/node_modules/dsh-tool-guardian   # Windows: cd %USERPROFILE%\.dsh\profiles\web\node_modules\dsh-tool-guardian
npm run setup
```

`npm run setup` finds your Python, then:
- finds the MCP servers you already use (Claude Desktop, Cursor, Windsurf, or a `.mcp.json` in the folder you are in) and lists what it would copy;
- asks before it writes `~/.tool-guardian/mcp.json`, and backs up any existing file first;
- runs the doctor: one `OK` / `WARN` / `FAIL` line per check, with a `fix:` line under each problem;
- ends with one `update:` line and the next step.

No other MCP client? Add servers one at a time instead. Everything after `--` is the server's command:

```bash
python tg_setup.py add files -- npx -y @modelcontextprotocol/server-filesystem ~/data
python tg_setup.py doctor
```

**3. Start a session and run the selftest (about 1 min).**

```bash
dsh web
```

Open a **new** session and type `/toolguardian`. It prints a report shaped like this one. The numbers are illustrative; yours shows your own servers:

```
tool-guardian 0.3.0-alpha.5 (router 0.3.0, python 3.11.9)
backends: 2 up, 0 down
  files              ok           9 tools
  git                ok           12 tools
router tools: 8 native tools; the router's own schemas cost ~540 tokens on every request
behind the router: 21 tools, ~6,900 tokens of schemas -> ~6,360 tokens freed on every request (92% smaller)
active groups: none (activate_group loads one)
output ladder: on -- 0 results shaped, 0 archived, 0 -> 0 chars since load
bypass watch: nudge -- 0 shell calls did a router tool's job this session (/toolguardian bypass)
update: none -- 0.3.0-alpha.5 is the newest

This proves the saving and that the backends start. It does NOT prove your model will call the router:
run a few real tasks, then /toolguardian bypass shows whether it did.
```

A server listed as `UNKNOWN` shows its real error in the same row. The doctor from step 2 usually says how to fix it.

**4. Give the model a real task (about 2 min).** Ask for something one of your servers does, without naming a tool. For example: *"What changed in this repo since yesterday?"* A model that uses the router calls `list_capabilities` -> `call_tool(server="git", ...)`. Then type:

```
/toolguardian bypass
```

```
tool-guardian (s20260926064210-8812): 2 router calls, 0 bypasses
  No bypasses recorded.
```

If the model ran the tool through the shell instead, the summary names the exact call it should have made:

```
tool-guardian (s20260926064210-8812): 0 router calls, 1 bypass
  1x bash ran git_log (mode nudge) -- next time: call_tool(server="git", tool="git_log", args={...})
```

In `nudge` mode (the default) the model sees the same line under the shell result. `bypass.mode: deny` blocks those calls instead (see [DSH settings](dsh-settings.md)). A model that keeps bypassing after a few tasks will not drive the router in this harness. Expose a small visible set of servers to it instead.

**5. Next session: nothing to set up again.** If a session loaded a group (`activate_group`), the next one *offers* it but does not load it. The DSH log says `tool-guardian: last session used groups git -- restore_groups() or /toolguardian restore loads them (not loaded automatically)`. `list_groups_with_costs` shows the same offer to the model. Type `/toolguardian restore` to take it, or ignore it.

## Path B: Claude Code (or any MCP client)

**1. Install.**

```bash
pip install tool-guardian
```

> PyPI currently has 0.1.0, which lacks setup, groups and the output ladder. Until 0.3 is published there, install from GitHub:
> `pip install "tool-guardian @ git+https://github.com/LuminariSoftwares/tool-guardian"`

**2. Copy your servers in and check them.**

```bash
tool-guardian-setup import      # asks before writing ~/.tool-guardian/mcp.json
tool-guardian-setup doctor      # ends with an "update:" line
tool-guardian --selftest        # starts the servers and prints the token saving, then an "update:" line
```

**3. Point Claude Code at the router, and only the router.**

```bash
claude mcp add tool-guardian -- tool-guardian
```

Then remove the servers you imported from Claude Code's own config, or they load twice.

**4. Optional: bypass detection.** A plain MCP server only sees calls made *to* it. It cannot see Claude Code's own `Bash` calls, so on its own it can only tell you:
- that a session **never called the router** (it logs a `session` row when the client connects);
- that a session **looked tools up but never called `call_tool`**.

To see real bypasses, add a `PreToolUse` hook to `.claude/settings.json` (project) or `~/.claude/settings.json` (user):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "command": "tool-guardian --hook-pretooluse" } ] }
    ]
  }
}
```

The hook reads the tool names that the router saved in `~/.tool-guardian/state.json` the last time it started. It never starts a server, and it never slows or breaks a shell call: any problem means "allow". It logs matches to the same call log. For each match it records only the tool and server names, never the command text. `--mode deny` (or `TOOL_GUARDIAN_HOOK_MODE=deny`) blocks the call and tells Claude the `call_tool(...)` to use. The hook follows Claude Code's documented `PreToolUse` input and output. It is tested with those payloads, not yet in a live Claude Code session.

**5. After a few real tasks:**

```bash
tool-guardian --bypass-summary               # the last 24 hours
tool-guardian --bypass-summary --session last
```
