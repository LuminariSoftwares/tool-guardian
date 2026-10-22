"""The router's own `tools/list` payload is the product's fixed per-request cost: a client re-sends
it whole on every request, whether the model touches a tool or not, so it is a budget and not a
preference (docs/adr/0003).

Measured the way the 2026-10-21 audit measures it, and on purpose end to end: start the real
entry point over stdio, complete the handshake, ask for `tools/list`, serialise exactly what came
back -- titles and annotations included, because the client receives those too -- and estimate at
3.5 characters per token. An in-process call to build_all_tools() would miss anything the serve()
path adds; `model_cost()` reads ~300 tokens low because it drops title/annotations by design.

0.7.0 measured 986 tokens here. The 2026-10-21 description rewrite took it to 1,781, which is
81 % of a 1,100-token budget spent again, and the saving it buys is the reason the router exists.
Raising BUDGET is a decision: write it in the CHANGELOG with the measurement.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUDGET = 1100
CHARS_PER_TOKEN = 3.5


def tools_list_tokens(tmp_path, servers=None):
    """(tokens, tools) from the real server over stdio. `servers` defaults to none configured,
    which is what a directory crawler and a client started before any config both see."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": servers or {}}), encoding="utf-8")
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "budget", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    run = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                         input="".join(json.dumps(m) + "\n" for m in requests),
                         capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, TOOL_GUARDIAN_HOME=str(tmp_path)), cwd=str(tmp_path))
    replies = {m["id"]: m for m in map(json.loads, run.stdout.splitlines()) if "id" in m}
    tools = replies[2]["result"]["tools"]
    return round(len(json.dumps(tools)) / CHARS_PER_TOKEN), tools


def test_tools_list_fits_the_budget(tmp_path):
    tokens, tools = tools_list_tokens(tmp_path)
    per_tool = ", ".join("%s %d" % (t["name"], round(len(json.dumps(t)) / CHARS_PER_TOKEN))
                         for t in tools)
    assert tokens <= BUDGET, (
        "tools/list is %d tokens, budget %d (%d over). Per tool: %s"
        % (tokens, BUDGET, tokens - BUDGET, per_tool))


def test_the_payload_measured_is_the_whole_router_surface(tmp_path):
    """Guard the measurement itself: a budget that silently stopped covering a tool, or the
    annotations, would pass while the real cost rose."""
    _, tools = tools_list_tokens(tmp_path)
    assert {t["name"] for t in tools} == {"list_capabilities", "describe_tool", "call_tool",
                                          "search_capabilities", "list_groups_with_costs",
                                          "retrieve_spill"}
    for tool in tools:
        assert tool.get("title") and tool.get("annotations"), tool["name"]
