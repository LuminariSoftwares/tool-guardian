"""2026-10-03 (P45 B4 finding): the catalogue prints tools as `server.tool` and models copy that
name into call_tool/describe_tool -- 18 ROUTER ERRORs in 25 bench sessions. And an EMPTY search
result was followed by "NEXT STEP: answer the user from this result", so a stale index became a
confident "it does not exist" (codebase-memory route 0 passes; graph_defines route 5/5)."""
import json
import sys

import pytest

import tool_guardian as tg
from test_tool_guardian import STUB_SERVER

EMPTY_JSON = '{"results":[],"raw_matches":[],"directories":{},"total_grep_matches":0,"total_results":0}'
STUB = STUB_SERVER.replace('"text": "pong"', "\"text\": %r" % EMPTY_JSON)


@pytest.fixture
def router(tmp_path, monkeypatch):
    stub = tmp_path / "stub_server.py"
    stub.write_text(STUB, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "stub": {"command": sys.executable, "args": [str(stub)]}}}), encoding="utf-8")
    monkeypatch.setattr(tg, "CALL_LOG", str(tmp_path / "calls.jsonl"))
    r = tg.Router(str(cfg), options={"spillDir": str(tmp_path / "spill")})
    r.start()
    tg.ROUTER_TOOLS = tg.build_all_tools(r.backends)
    return r


def test_call_tool_accepts_the_catalogue_name(router):
    out = router.handle("call_tool", {"server": "stub", "tool": "stub.echo", "args": {"text": "hi"}})
    assert out.startswith("echo: hi") and "has no tool" not in out


def test_describe_tool_accepts_the_catalogue_name(router):
    out = router.handle("describe_tool", {"server": "stub", "tool": "stub.echo"})
    assert "has no tool" not in out and "tool echo" in out


def test_unknown_name_still_refused(router):
    with pytest.raises(RuntimeError, match="has no tool"):   # the stdio layer prints it as ROUTER ERROR
        router.handle("call_tool", {"server": "stub", "tool": "stub.nope", "args": {}})


@pytest.mark.parametrize("text,want", [
    (EMPTY_JSON, True), ("[]", True), ('{"results": [], "count": 0}', True), ("no results found", True),
    ('{"results": [{"name": "x"}], "total_results": 1}', False), ("pong", False),
    ('{"error": "project not found"}', False), ('{"count": 0, "items": [{"a": 1}]}', False), ("", False),
])
def test_looks_empty(text, want):
    fn = getattr(tg, "looks_empty", None)
    assert fn is not None, "tool_guardian.looks_empty is missing"
    assert fn(text) is want


def test_empty_result_says_not_proof_and_names_list_capabilities(router):
    out = router.handle("call_tool", {"server": "stub", "tool": "ping", "args": {}})
    nxt = out.split("NEXT STEP:")[-1]
    assert "not proof" in nxt and "list_capabilities" in nxt
    assert "answer the user from this result" not in nxt


def test_nonempty_result_keeps_answer_now(router):
    out = router.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": "hi"}})
    assert out.rstrip().endswith("NEXT STEP: answer the user from this result.")
