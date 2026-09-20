"""Integration tests for tool_guardian.

These use a STUB MCP server (a tiny stdio server written to a temp file and run
with the same interpreter) so the whole path is exercised for real: config load
-> start backend -> initialize/tools/list handshake -> list/describe/call. No
network, no external deps.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tool_guardian as tg  # noqa: E402


# A minimal but real MCP stdio server: initialize, tools/list, tools/call(echo).
STUB_SERVER = '''
import json, sys
TOOLS = [
    {"name": "echo", "description": "Echo back the text you send.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "ping", "description": "Return pong.",
     "inputSchema": {"type": "object", "properties": {}}},
]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "stub", "version": "1"}}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        name = p.get("name"); args = p.get("arguments") or {}
        if name == "echo":
            res = {"content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}]}
        elif name == "ping":
            res = {"content": [{"type": "text", "text": "pong"}]}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                             "error": {"code": -32601, "message": "no tool " + str(name)}}) + "\\n")
            sys.stdout.flush(); continue
    elif mid is None:
        continue
    else:
        res = {}
    if mid is not None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
        sys.stdout.flush()
'''


@pytest.fixture
def router(tmp_path):
    stub = tmp_path / "stub_server.py"
    stub.write_text(STUB_SERVER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "stub": {"command": sys.executable, "args": [str(stub)],
                 "description": "a test echo server"},
    }}), encoding="utf-8")
    r = tg.Router(str(cfg))
    r.start()
    tg.ROUTER_TOOLS = tg.build_router_tools(r.backends)
    return r


def test_backend_starts_and_lists_tools(router):
    b = router.backends["stub"]
    assert b.status == "ok"
    assert {t["name"] for t in b.tools} == {"echo", "ping"}


def test_catalogue_all_and_one(router):
    all_cat = router.catalogue()
    assert "[stub]" in all_cat and "echo" in all_cat
    one = router.catalogue("stub")
    assert "stub.echo" in one
    assert "NEXT STEP" in one  # the result must nudge the model to call_tool


def test_describe_tool(router):
    out = router.handle("describe_tool", {"server": "stub", "tool": "echo"})
    # 0.3.0: every result ends with an exact NEXT STEP; the schema is the part before it
    body, _, nxt = out.partition("\n\nNEXT STEP: ")
    assert nxt.startswith('call_tool(server="stub", tool="echo"')
    schema = json.loads(body)
    assert schema["name"] == "echo"
    assert "text" in schema["inputSchema"]["properties"]


def test_call_tool_roundtrip(router):
    out = router.handle("call_tool", {"server": "stub", "tool": "echo",
                                      "args": {"text": "hi"}})
    assert "echo: hi" in out


def test_call_tool_accepts_json_string_args(router):
    # models often send args as a JSON string, not an object
    out = router.handle("call_tool", {"server": "stub", "tool": "echo",
                                      "args": '{"text": "strung"}'})
    assert "echo: strung" in out


def test_list_capabilities_accepts_query_alias(router):
    # a model naming the server `query` instead of `server` must still work
    out = router.handle("list_capabilities", {"query": "stub"})
    assert "stub.echo" in out


def test_unknown_backend_is_loud_not_empty(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "broken": {"command": "this_command_does_not_exist_xyz", "args": []},
    }}), encoding="utf-8")
    r = tg.Router(str(cfg))
    r.start()
    b = r.backends["broken"]
    assert b.status == "UNKNOWN"
    cat = r.catalogue("broken")
    assert "UNKNOWN" in cat
    assert "empty tool list" in cat  # explicitly tells the model this is not []


def test_call_on_dead_backend_raises_loudly(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "broken": {"command": "nope_xyz", "args": []}}}), encoding="utf-8")
    r = tg.Router(str(cfg))
    r.start()
    # handle() deliberately does NOT swallow a dead-backend call: it raises, and
    # serve() turns that into a visible isError tool result. The failure must be
    # loud with UNKNOWN wording, never a fake success.
    with pytest.raises(RuntimeError) as e:
        r.handle("call_tool", {"server": "broken", "tool": "whatever"})
    assert "UNKNOWN" in str(e.value)


def test_router_cost_is_small_and_fixed_and_scales_against_backends():
    # The router's own tool payload is a small, ~constant cost regardless of how
    # many servers hide behind it -- that fixed cost is the whole value prop.
    # (With a toy 2-tool backend the router can cost MORE; the win is at scale,
    # so we test the mechanism, not a false always-cheaper inequality.)
    router_cost = tg.est_tokens(tg.build_router_tools({}))
    assert 0 < router_cost < 1500
    small = tg.est_tokens([{"name": "a", "description": "x"}])
    big = tg.est_tokens([{"name": "t%d" % i,
                          "description": "a tool with a reasonably long description " * 3,
                          "inputSchema": {"type": "object", "properties": {
                              "p%d" % j: {"type": "string"} for j in range(8)}}}
                         for i in range(40)])
    assert big > small
    assert big > router_cost  # 40 real tools dwarf the 3 router tools -> the win


def test_no_config_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no mcp.json here
    monkeypatch.delenv("TOOL_GUARDIAN_CONFIG", raising=False)
    r = tg.Router()
    r.start()
    assert r.backends == {}
    assert r.catalogue() == "no backends configured"


def test_unsupported_url_backend(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "remote": {"url": "https://example.com/mcp"}}}), encoding="utf-8")
    r = tg.Router(str(cfg))
    r.start()
    assert r.backends["remote"].status == "UNSUPPORTED"
