"""The call_tool reply a client receives must not move when features are added around it (P141 S0).

tests/fixtures/ladder_golden.json holds the EXACT text Router.handle("call_tool", ...) returns for a
fake in-process backend (no subprocess; Backend.call is replaced) in four cases: a small result, a
~40 KB plain-text result, a ~40 KB JSON result, and an is_error result. The 40 KB inputs are built
from fixed formulas, never random. Regenerate on purpose, from a commit whose output you mean to record:

    python tests/test_ladder_identity.py --write

Normalisation: none. The reply carries no path and no timing (the spill directory is only written, never
named in the reply) and the spill id is a content hash, so it stays literal. Each golden entry also records
the spill id and the original length, so a change in the input generator shows up as a separate failure.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "ladder_golden.json"


def _plain(n=40000):
    lines = []
    i = 0
    while sum(len(x) + 1 for x in lines) < n:
        lines.append("line %05d: record %d value %d status %s" % (i, i * 7 % 101, i * i % 9973,
                                                                  "ok" if i % 5 else "retry"))
        i += 1
    return "\n".join(lines)


def _json_text(n=40000):
    rows = []
    i = 0
    while len(json.dumps(rows, indent=2)) < n:
        rows.append({"id": i, "name": "item-%d" % i, "score": (i * 37) % 1000,
                     "tags": ["a%d" % (i % 3), "b%d" % (i % 7)], "active": i % 2 == 0})
        i += 1
    return json.dumps({"total": len(rows), "results": rows}, indent=2)


CASES = {
    "small": ({"content": [{"type": "text", "text": "hello from the fake backend"}]}, "echo"),
    "plain_40k": ({"content": [{"type": "text", "text": _plain()}]}, "read_log"),
    "json_40k": ({"content": [{"type": "text", "text": _json_text()}]}, "list_items"),
    "error": ({"content": [{"type": "text", "text": "backend failed: no such file 'missing.txt'"}],
               "isError": True}, "read_file"),
}


def _reply(name, tmp_dir):
    result, tool = CASES[name]
    os.environ["TOOL_GUARDIAN_SPILL_DIR"] = str(tmp_dir)
    r = tg.Router("", options={"spillDir": str(tmp_dir)})
    r._ceiling = None
    b = tg.Backend("fake", {"command": "none"})
    b.tools = [{"name": tool, "description": "fake tool", "inputSchema": {"type": "object"}}]
    b.status = "ok"
    b.call = lambda t, a: result
    r.backends["fake"] = b
    text = r.handle("call_tool", {"server": "fake", "tool": tool, "args": {}})
    return {"reply": text, "original_chars": len(result["content"][0]["text"]),
            "spill_id": r._last_meta.get("spill_id", ""), "rule": r._last_meta.get("rule", "")}


def snapshot(base) -> dict:
    out = {}
    for name in CASES:
        d = Path(base) / name
        d.mkdir(parents=True, exist_ok=True)
        out[name] = _reply(name, d)
    return out


def test_call_tool_replies_are_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_GUARDIAN_SPILL_DIR", str(tmp_path / "unused"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    now = snapshot(tmp_path)
    assert sorted(now) == sorted(golden)
    for name in golden:
        assert now[name]["original_chars"] == golden[name]["original_chars"], (name, "input generator moved")
        assert now[name]["reply"] == golden[name]["reply"], name
        assert now[name]["spill_id"] == golden[name]["spill_id"], name
        assert now[name]["rule"] == golden[name]["rule"], name


def test_cases_exercise_the_ladder(tmp_path):
    now = snapshot(tmp_path)
    assert now["small"]["spill_id"] == ""
    assert now["plain_40k"]["spill_id"].startswith("sp_")
    assert now["json_40k"]["spill_id"].startswith("sp_")
    assert "isError" not in now["error"]["reply"] and "backend failed" in now["error"]["reply"]


if __name__ == "__main__":
    if "--write" in sys.argv:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            GOLDEN.write_text(json.dumps(snapshot(td), indent=1, sort_keys=True) + "\n", encoding="utf-8")
