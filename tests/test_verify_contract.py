"""Contract probe for the 0.4 router verification layer (P36 T1-T3).

Written by the overseer FROM THE CONTRACT, before the code existed, and seen red
first. It is the proof for three features, not a unit test of their internals:

  T1  search_capabilities(query, server?, limit?) -- keyword discovery, one line per tool
  T2  call_tool argument validation -- a call that does not match the tool's schema is
      NOT sent to the backend; safe coercions ("2" -> 2) are applied silently
  T3  repeated-call guard -- the 3rd identical call with an identical result gets a
      warning; the 5th is not run again

The stub MCP server counts every tools/call per tool in a JSON file, so a test can
prove the backend was (or was not) reached.
"""
import json
import sys

import pytest

import tool_guardian as tg

STUB = r'''
import json, os, sys
COUNTS = os.environ["TG_STUB_COUNTS"]
TOOLS = [
    {"name": "add", "description": "Add two whole numbers and return the sum.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                     "required": ["a", "b"]}},
    {"name": "set_mode", "description": "Set the fan mode.",
     "inputSchema": {"type": "object",
                     "properties": {"level": {"type": "string", "enum": ["low", "high"]}},
                     "required": ["level"]}},
    {"name": "const", "description": "Always returns the same status text.",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "tick", "description": "Returns an increasing counter.",
     "inputSchema": {"type": "object", "properties": {}}},
]
def bump(name):
    try:
        c = json.load(open(COUNTS))
    except Exception:
        c = {}
    c[name] = c.get(name, 0) + 1
    json.dump(c, open(COUNTS, "w"))
    return c[name]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "math", "version": "1"}}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        n = bump(name)
        if name == "add":
            text = "sum: " + str(args.get("a") + args.get("b"))
        elif name == "set_mode":
            text = "mode " + str(args.get("level"))
        elif name == "const":
            text = "status: idle"
        else:
            text = "tick " + str(n)
        res = {"content": [{"type": "text", "text": text}]}
    elif mid is None:
        continue
    else:
        res = {}
    if mid is not None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
        sys.stdout.flush()
'''

OTHER = r'''
import json, sys
TOOLS = [{"name": "git_log", "description": "Show recent git commits.",
          "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "other", "version": "1"}}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        res = {"content": [{"type": "text", "text": "abc123 first commit"}]}
    elif mid is None:
        continue
    else:
        res = {}
    if mid is not None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
        sys.stdout.flush()
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    counts = tmp_path / "counts.json"
    monkeypatch.setenv("TG_STUB_COUNTS", str(counts))
    monkeypatch.delenv("TG_VALIDATE_ARGS", raising=False)
    monkeypatch.delenv("TG_LOOP_GUARD", raising=False)
    (tmp_path / "math_stub.py").write_text(STUB, encoding="utf-8")
    (tmp_path / "other_stub.py").write_text(OTHER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "math": {"command": sys.executable, "args": [str(tmp_path / "math_stub.py")]},
        "other": {"command": sys.executable, "args": [str(tmp_path / "other_stub.py")]},
    }}), encoding="utf-8")
    monkeypatch.setattr(tg, "CALL_LOG", str(tmp_path / "calls.jsonl"))
    r = tg.Router(str(cfg), options={"spillDir": str(tmp_path / "spill")})
    r.start()
    tg.ROUTER_TOOLS = tg.build_all_tools(r.backends)

    def count(tool):
        try:
            return json.loads(counts.read_text(encoding="utf-8")).get(tool, 0)
        except (OSError, ValueError):
            return 0
    return r, count


def call(r, tool, args, server="math"):
    return r.handle("call_tool", {"server": server, "tool": tool, "args": args})


def tool_lines(text):
    return [ln for ln in text.splitlines() if ln[:1].isalpha() and "." in ln.split(":")[0]
            and ":" in ln]


# ---- T1 search_capabilities -------------------------------------------------------

def test_t1_search_tool_is_advertised(env):
    names = {t["name"]: t for t in tg.build_router_tools(env[0].backends)}
    assert "search_capabilities" in names
    assert names["search_capabilities"]["inputSchema"].get("required") == ["query"]


def test_t1_search_ranks_the_matching_tool_first(env):
    out = env[0].handle("search_capabilities", {"query": "add numbers"})
    lines = tool_lines(out)
    assert lines and lines[0].startswith("math.add: "), out
    assert "NEXT STEP" in out


def test_t1_search_no_match_points_to_list_capabilities(env):
    out = env[0].handle("search_capabilities", {"query": "zzqx"})
    assert "no tools match" in out and "list_capabilities" in out


def test_t1_search_server_filter(env):
    out = env[0].handle("search_capabilities", {"query": "git commits", "server": "math"})
    assert "other.git_log" not in out
    out2 = env[0].handle("search_capabilities", {"query": "git commits"})
    assert "other.git_log" in out2


def test_t1_search_limit(env):
    out = env[0].handle("search_capabilities", {"query": "returns the status counter", "limit": 1})
    assert len(tool_lines(out)) == 1, out


# ---- T2 argument validation -------------------------------------------------------

def test_t2_validate_args_pure():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert tg.validate_args(schema, {"n": "7"}) == (True, {"n": 7}, [])
    ok, _fixed, problems = tg.validate_args(schema, {})
    assert ok is False and problems
    assert tg.validate_args({}, {"x": 1}) == (True, {"x": 1}, [])


def test_t2_missing_required_is_not_sent(env):
    r, count = env
    out = call(r, "add", {"a": 1})
    assert "was not sent" in out and "missing required argument 'b'" in out, out
    assert count("add") == 0
    assert r.stats.get("schema_rejects") == 1


def test_t2_safe_coercion_reaches_backend_as_integers(env):
    r, count = env
    out = call(r, "add", {"a": "2", "b": "3"})
    assert "sum: 5" in out, out
    assert count("add") == 1
    assert r.stats.get("schema_coercions", 0) >= 1


def test_t2_unknown_key_refused_when_schema_is_closed(env):
    r, count = env
    out = call(r, "add", {"a": 1, "b": 2, "c": 3})
    assert "was not sent" in out and "unknown argument 'c'" in out, out
    assert count("add") == 0


def test_t2_enum_violation_lists_allowed_values(env):
    r, count = env
    out = call(r, "set_mode", {"level": "extreme"})
    assert "was not sent" in out and "low" in out and "high" in out, out
    assert count("set_mode") == 0


def test_t2_switch_off_fails_open(env, monkeypatch):
    r, count = env
    monkeypatch.setenv("TG_VALIDATE_ARGS", "0")
    call(r, "add", {"a": 1, "b": 2, "c": 3})
    assert count("add") == 1


# ---- T3 repeated-call guard -------------------------------------------------------

WARN = "[tool-guardian: this exact call has now run 3 times with the same result."
BLOCK = "[tool-guardian: this exact call was not run again"


def test_t3_third_identical_call_warns(env):
    r, _count = env
    outs = [call(r, "const", {"q": "x"}) for _ in range(3)]
    assert WARN not in outs[0] and WARN not in outs[1]
    assert WARN in outs[2], outs[2]
    assert r.stats.get("loop_warnings") == 1


def test_t3_changing_results_never_warn(env):
    r, _count = env
    outs = [call(r, "tick", {}) for _ in range(4)]
    assert not any("[tool-guardian: this exact call" in o for o in outs)


def test_t3_fifth_identical_call_is_not_run(env):
    r, count = env
    outs = [call(r, "const", {"q": "y"}) for _ in range(5)]
    assert BLOCK in outs[4], outs[4]
    assert count("const") == 4
    assert r.stats.get("loop_blocks") == 1


def test_t3_switch_off(env, monkeypatch):
    r, _count = env
    monkeypatch.setenv("TG_LOOP_GUARD", "0")
    outs = [call(r, "const", {"q": "z"}) for _ in range(3)]
    assert WARN not in outs[2]
