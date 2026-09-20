"""0.3.0 router behaviour: the output ladder + archive on call_tool results, an exact
NEXT STEP on every result, the group manifest, and the call log. Same STUB MCP server
as test_tool_guardian.py (its `echo` tool lets a test choose the result's size)."""
import json
import re
import sys

import pytest

import tool_guardian as tg
from test_tool_guardian import STUB_SERVER

pytestmark = pytest.mark.skipif(
    tg.tg_ladder is None or tg.tg_spill is None or tg.tg_groups is None,
    reason="tg_ladder.py / tg_spill.py / tg_groups.py are not beside tool_guardian.py")

BIG = "\n".join("line %05d output text" % i for i in range(1, 3001))


@pytest.fixture
def router(tmp_path, monkeypatch):
    stub = tmp_path / "stub_server.py"
    stub.write_text(STUB_SERVER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "stub": {"command": sys.executable, "args": [str(stub)]}}}), encoding="utf-8")
    monkeypatch.setattr(tg, "CALL_LOG", str(tmp_path / "calls.jsonl"))
    r = tg.Router(str(cfg), options={"spillDir": str(tmp_path / "spill")})
    r.start()
    tg.ROUTER_TOOLS = tg.build_all_tools(r.backends)
    return r


def call(router, text):
    return router.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": text}})


def test_small_result_is_untouched_and_says_answer_now(router):
    out = call(router, "hi")
    assert out.startswith("echo: hi\n\nNEXT STEP: ")
    assert "answer the user" in out and "[tool-guardian:" not in out


def test_big_result_is_shortened_archived_and_fully_recoverable(router, tmp_path):
    out = call(router, BIG)
    assert len(out) < 9000 < len(BIG)
    m = re.search(r"\[tool-guardian: showing \d+ of (\d+) chars\. Full original archived as (sp_[0-9a-f]{12})", out)
    assert m and int(m.group(1)) == len("echo: " + BIG)
    assert 'retrieve_spill(id="%s"' % m.group(2) in out.split("NEXT STEP:")[-1]
    assert "line 01500 output text" not in out                       # genuinely dropped ...
    got = router.handle("retrieve_spill", {"id": m.group(2), "grep": "line 01500 "})
    assert "L1500: line 01500 output text" in got                    # ... and genuinely recoverable
    assert (tmp_path / "spill" / (m.group(2) + ".txt")).read_text(encoding="utf-8") == "echo: " + BIG


def test_retrieve_spill_pages_with_an_exact_next_call(router):
    sid = re.search(r"sp_[0-9a-f]{12}", call(router, BIG)).group(0)
    got = router.handle("retrieve_spill", {"id": sid, "max_lines": 10})
    assert got.startswith("[%s: lines 1-10 of 3000]" % sid)
    assert 'NEXT STEP: more remains -- retrieve_spill(id="%s", start_line=11)' % sid in got
    assert "retrieve_spill failed" in router.handle("retrieve_spill", {"id": "../../etc/passwd"})


def test_describe_tool_names_the_exact_call(router):
    out = router.handle("describe_tool", {"server": "stub", "tool": "echo"})
    assert out.rstrip().endswith('NEXT STEP: call_tool(server="stub", tool="echo", args={"text": <text>})')


def test_wrong_names_point_at_the_exact_recovery_call(router):
    assert router.handle("describe_tool", {"server": "nope", "tool": "x"}).endswith("NEXT STEP: list_capabilities()")
    assert router.handle("describe_tool", {"server": "stub", "tool": "x"}).endswith('NEXT STEP: list_capabilities(server="stub")')


def test_group_manifest_prices_groups_and_names_only_tools_this_path_has(router):
    out = router.handle("list_groups_with_costs", {})
    assert re.search(r"(?m)^stub\s+2 tools\s+~\d+ tokens", out)
    assert "activate_group" not in out and "call_tool" in out.splitlines()[-1]
    router.native_groups = True                                      # what the DSH bridge sets
    assert "activate_group(group=" in router.handle("list_groups_with_costs", {}).splitlines()[-1]


def test_call_log_records_the_call_but_never_argument_values(router, tmp_path):
    call(router, "hi-SECRET-value")
    call(router, BIG)
    rows = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["tool"] for r in rows] == ["call_tool", "call_tool"]
    assert rows[0]["arg_keys"] == ["args", "server", "tool"] and rows[0]["target"] == "echo"
    assert rows[1]["rule"] in ("shell", "sampled") and rows[1]["spill_id"].startswith("sp_")
    assert rows[1]["original_chars"] > rows[1]["final_chars"] > 0
    assert "SECRET" not in (tmp_path / "calls.jsonl").read_text(encoding="utf-8")


def test_ladder_off_keeps_the_old_cap_and_says_so(router, monkeypatch):
    monkeypatch.setattr(tg, "LADDER_ON", False)
    out = call(router, "z" * 30000)
    assert "cut at 20000 of 30006 chars" in out and "sp_" not in out


def test_nothing_lossy_without_an_archive(router, monkeypatch):
    class Broken:
        def save(self, *a, **k):
            raise OSError("disk full")
    monkeypatch.setattr(router, "spill_store", lambda: Broken())
    out = call(router, BIG[:15000])
    assert out.startswith("echo: " + BIG[:14000])                    # the ORIGINAL came back
    assert "[tool-guardian: showing" not in out


def test_raw_results_env_restores_the_pre_030_envelope(router, monkeypatch):
    monkeypatch.setattr(tg, "RAW_RESULTS", True)
    out = call(router, "hi")
    assert json.loads(out)["content"][0]["text"] == "echo: hi"


def test_router_advertises_the_two_new_tools():
    names = [t["name"] for t in tg.build_all_tools({})]
    assert names[:3] == ["list_capabilities", "describe_tool", "call_tool"]
    assert "list_groups_with_costs" in names and "retrieve_spill" in names
