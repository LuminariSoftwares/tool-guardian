"""Contract probe for P36 T4 -- recall tracking (overseer-written, before the code).

When the output ladder shortens a result and archives the original, the router counts it
per tool; when the model reads an archive back with retrieve_spill, the router counts the
recall against the tool that produced it. A tool whose shortened results keep getting
recalled is being shortened too hard.
"""
import json
import re
import sys

import pytest

import tool_guardian as tg
from test_tool_guardian import STUB_SERVER

pytestmark = pytest.mark.skipif(
    tg.tg_ladder is None or tg.tg_spill is None,
    reason="tg_ladder.py / tg_spill.py are not beside tool_guardian.py")

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


def spill_id(text):
    m = re.search(r"sp_[0-9a-f]{12}", text)
    assert m, text[-300:]
    return m.group(0)


def test_recalls_start_empty(router):
    assert router.stats.get("recalls") == {}


def test_lossy_result_counts_as_spilled(router):
    out = router.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": BIG}})
    spill_id(out)
    assert router.stats["recalls"] == {"echo": {"spilled": 1, "recalled": 0}}


def test_first_recall_counts_once(router):
    out = router.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": BIG}})
    sid = spill_id(out)
    router.handle("retrieve_spill", {"id": sid, "grep": "line 01500 "})
    router.handle("retrieve_spill", {"id": sid, "start_line": 10})
    assert router.stats["recalls"]["echo"] == {"spilled": 1, "recalled": 1}


def test_small_result_is_not_counted(router):
    router.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": "hi"}})
    assert router.stats["recalls"] == {}


def test_unknown_id_changes_nothing(router):
    router.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": BIG}})
    router.handle("retrieve_spill", {"id": "sp_000000000000"})
    router.handle("retrieve_spill", {"id": "banana"})
    assert router.stats["recalls"] == {"echo": {"spilled": 1, "recalled": 0}}


def test_native_tool_results_through_shape_are_counted(router):
    shaped, meta = router.shape(BIG, tool="bash")
    assert meta["spill_id"]
    router.handle("retrieve_spill", {"id": meta["spill_id"]})
    assert router.stats["recalls"]["bash"] == {"spilled": 1, "recalled": 1}


def test_unarchived_shortening_is_not_counted(router):
    # archive=False: the harness kept the original itself, so there is no id to recall
    router.shape(BIG, tool="pwsh", archive=False)
    assert "pwsh" not in router.stats["recalls"]
