"""The "Later" list from the 2026-10-10 review triage, one section per item (2026-10-14).
Each test here failed on main before its change."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402


# ---- 1. nested validate_args -----------------------------------------------------

NESTED = {"type": "object", "required": ["params"], "properties": {
    "params": {"type": "object", "required": ["filters"], "properties": {
        "limit": {"type": "integer"},
        "filters": {"type": "array", "items": {
            "type": "object", "required": ["field", "op"], "additionalProperties": False,
            "properties": {"field": {"type": "string"},
                           "op": {"type": "string", "enum": ["eq", "lt", "gt"]},
                           "value": {"type": "number"}}}}}}}}


def _filters(*extra):
    return [{"field": "a", "op": "eq", "value": 1}, {"field": "b", "op": "lt", "value": 2}, *extra]


def test_nested_valid_call_passes_and_coerces_safely():
    ok, fixed, problems = tg.validate_args(NESTED, {"params": {"limit": "5", "filters": _filters(
        {"field": "c", "op": "gt", "value": "3.5"})}})
    assert (ok, problems) == (True, [])
    assert fixed["params"]["limit"] == 5 and fixed["params"]["filters"][2]["value"] == 3.5


def test_nested_enum_fault_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters(
        {"field": "c", "op": "like", "value": 1})}})
    assert not ok and problems == ["argument 'params.filters[2].op' must be one of: eq, lt, gt"]


def test_nested_missing_required_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters({"field": "c"})}})
    assert not ok and problems == ["missing required argument 'params.filters[2].op' (string)"]


def test_nested_type_fault_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters(
        {"field": "c", "op": "eq", "value": "lots"})}})
    assert not ok and problems == ["argument 'params.filters[2].value' should be number, got str"]


def test_nested_closed_object_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": [
        {"field": "a", "op": "eq", "colour": "red"}]}})
    assert not ok and problems == ["unknown argument 'params.filters[0].colour'; allowed: field, op, value"]


# ---- 2. respawn a dead backend ---------------------------------------------------

# Answers the handshake and ONE tools/call, then exits. Every spawn counts its own calls,
# and appends one line per spawn to $TG_SPAWN_LOG so the test can count initialize handshakes.
DIES_AFTER_FIRST_CALL = '''
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        with open(os.environ["TG_SPAWN_LOG"], "a") as fh:
            fh.write("initialize\\n")
    if mid is None:
        continue
    if method == "tools/list":
        res = {"tools": [{"name": "once", "description": "answers, then the server exits",
                          "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        res = {"content": [{"type": "text", "text": "answered pid %d" % os.getpid()}]}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
    sys.stdout.flush()
    if method == "tools/call":
        sys.exit(0)
'''


@pytest.fixture
def dying(tmp_path, monkeypatch):
    from test_review_1010 import _config, _kill_all
    log = tmp_path / "spawns.log"
    monkeypatch.setenv("TG_SPAWN_LOG", str(log))
    r = tg.Router(str(_config(tmp_path, {"flaky": DIES_AFTER_FIRST_CALL})))
    r.start()
    yield r, log
    _kill_all(r)


def _call_once(r):
    return r.handle("call_tool", {"server": "flaky", "tool": "once"})


def test_dead_backend_is_respawned_once_after_1s_then_marked_down(dying):
    import time
    r, log = dying
    b = r.backends["flaky"]
    first = _call_once(r)
    assert "answered pid" in first and r.stats["backend_respawns"] == 0
    b.proc.wait(timeout=5)                      # the backend has died
    t0 = time.monotonic()
    second = _call_once(r)                      # respawned, handshake re-sent, call answered
    took = time.monotonic() - t0
    assert "answered pid" in second and second.split()[2] != first.split()[2], (first, second)
    assert took >= 1.0, "respawn did not wait the 1 s backoff (%.2f s)" % took
    assert log.read_text().count("initialize") == 2
    assert r.stats["backend_respawns"] == 1 and b.respawns == 1
    b.proc.wait(timeout=5)                      # dead again, inside 60 s of the first failure
    with pytest.raises(RuntimeError, match="DOWN"):
        _call_once(r)
    assert b.status == "DOWN" and log.read_text().count("initialize") == 2   # no third spawn
    with pytest.raises(RuntimeError, match="DOWN"):
        _call_once(r)
    assert r.stats["backend_respawns"] == 1
    assert "[flaky] DOWN" in r.catalogue()


def test_second_respawn_outside_the_window_waits_5s(dying, monkeypatch):
    r, _log = dying
    b = r.backends["flaky"]
    monkeypatch.setattr(tg, "RESPAWN_WINDOW", 0.0)    # every failure is the first in its window
    waits = []
    monkeypatch.setattr(tg, "_backoff_sleep", lambda s: waits.append(round(s)))
    for _ in range(3):
        assert "answered pid" in _call_once(r)
        b.proc.wait(timeout=5)
    assert waits == [1, 5] and b.respawns == 2 and r.stats["backend_respawns"] == 2


def test_respawns_are_in_the_bridge_stats():
    assert tg.Router("").stats["backend_respawns"] == 0
