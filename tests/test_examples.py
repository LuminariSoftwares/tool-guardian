"""examples/: each client config parses, names the router the same way, and the command it
starts speaks MCP. The full client runs (a real Claude Code / OpenCode / Cline / Continue
against a scripted model) are tests/clients/run_clients.py; this is the part every OS can run."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _server_commands():
    """(client, argv) for every example, read the way that client reads it."""
    cc = json.loads((EXAMPLES / "claude-code" / "project.mcp.json").read_text(encoding="utf-8"))
    entry = cc["mcpServers"]["tool-guardian"]
    yield "claude-code", [entry["command"], *entry.get("args", [])]

    oc = json.loads((EXAMPLES / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    entry = oc["mcp"]["tool-guardian"]
    assert entry["type"] == "local" and entry["enabled"] is True
    yield "opencode", list(entry["command"])

    cline = json.loads((EXAMPLES / "cline" / "cline_mcp_settings.json").read_text(encoding="utf-8"))
    entry = cline["mcpServers"]["tool-guardian"]
    assert entry["disabled"] is False
    yield "cline", [entry["command"], *entry.get("args", [])]

    # Continue's YAML, read without a YAML library: the one mcpServers entry
    text = (EXAMPLES / "continue" / "tool-guardian.yaml").read_text(encoding="utf-8")
    lines = [line.strip() for line in text[text.index("mcpServers:"):].splitlines()[1:] if line.strip()]
    fields = dict(line.lstrip("- ").split(":", 1) for line in lines)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    assert fields["name"] == "tool-guardian" and fields["args"] == "[]"
    yield "continue", [fields["command"]]


def test_every_example_starts_the_same_command():
    commands = dict(_server_commands())
    assert set(commands) == {"claude-code", "opencode", "cline", "continue"}
    assert all(argv == ["tool-guardian"] for argv in commands.values()), commands


def test_cline_auto_approves_discovery_only():
    cline = json.loads((EXAMPLES / "cline" / "cline_mcp_settings.json").read_text(encoding="utf-8"))
    approved = set(cline["mcpServers"]["tool-guardian"]["autoApprove"])
    assert "call_tool" not in approved and approved <= {"list_capabilities", "search_capabilities", "describe_tool"}


def test_the_example_command_speaks_mcp(tmp_path):
    """`tool-guardian` with no arguments, as every example starts it (run from this checkout)."""
    from test_tool_guardian import STUB_SERVER
    stub = tmp_path / "stub_server.py"
    stub.write_text(STUB_SERVER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"stub": {"command": sys.executable, "args": [str(stub)]}}}),
                   encoding="utf-8")
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "examples-test"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_capabilities", "arguments": {}}},
    ]
    env = dict(os.environ, TOOL_GUARDIAN_CONFIG=str(cfg))
    r = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py")],
                       input="".join(json.dumps(f) + "\n" for f in frames),
                       capture_output=True, text=True, env=env, timeout=60)
    replies = {m["id"]: m for m in (json.loads(line) for line in r.stdout.splitlines() if line.strip()) if "id" in m}
    assert replies[1]["result"]["serverInfo"]["name"]
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert {"list_capabilities", "search_capabilities", "describe_tool", "call_tool"} <= names
    text = replies[3]["result"]["content"][0]["text"]
    assert "[stub]" in text and "echo" in text
