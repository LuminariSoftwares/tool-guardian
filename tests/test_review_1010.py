"""Regression tests for the 2026-10-10 review: each test failed on main (cda2b8f) before its fix.

1  START/CALL timeouts are enforced (a backend that never answers cannot hang the router)
2  summarize: search-only sessions are stalled; only call_tool counts as invoked
3  tg_spill: sidecar written before the text, rebuilt when missing/invalid; prune survives a delete
4  archive failures are counted
5  call_tool without `server` resolves "server.tool" or a unique bare tool name
6  retrieve_spill past the end says so
7  TOOL_GUARDIAN_* tunables from .env are honoured
8  --selftest: per-server cost, non-zero exit on any failed backend, 160-char errors
"""
import json
import os
import pathlib
import re
import sys
import threading
import time

import pytest

import tg_spill
import tg_state
import tool_guardian as tg
from test_tool_guardian import STUB_SERVER

# Reads stdin forever and never writes a byte: the backend that hung start for 21 s.
SILENT_SERVER = "import sys\nfor line in sys.stdin:\n    pass\n"

# Answers the handshake, then never answers tools/call.
MUTE_ON_CALL_SERVER = '''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if mid is None or method == "tools/call":
        continue
    res = {"tools": [{"name": "wait", "description": "never returns",
                      "inputSchema": {"type": "object", "properties": {}}}]} if method == "tools/list" else {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
    sys.stdout.flush()
'''

BIG = "\n".join("line %05d output text" % i for i in range(1, 3001))


def _config(tmp_path, servers):
    """servers: {name: script source or a ready spec dict}."""
    block = {}
    for name, src in servers.items():
        if isinstance(src, dict):
            block[name] = src
            continue
        path = tmp_path / ("%s.py" % name)
        path.write_text(src, encoding="utf-8")
        block[name] = {"command": sys.executable, "args": [str(path)]}
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": block}), encoding="utf-8")
    return cfg


def _kill_all(router):
    for b in router.backends.values():
        if b.proc is not None and b.proc.poll() is None:
            b.proc.kill()


def _run_with_watchdog(fn, limit=15.0):
    """Run fn in a daemon thread; return (finished, seconds, result)."""
    box = {}

    def target():
        box["result"] = fn()

    t = threading.Thread(target=target, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(limit)
    return (not t.is_alive()), time.monotonic() - t0, box.get("result")


# ---------------------------------------------------------------- 1 timeouts --

def test_start_timeout_kills_a_silent_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "START_TIMEOUT", 1.0)
    r = tg.Router(str(_config(tmp_path, {"silent": SILENT_SERVER, "stub": STUB_SERVER})))
    try:
        finished, took, _ = _run_with_watchdog(r.start)
        assert finished, "Router.start still blocked after 15 s with START_TIMEOUT=1"
        assert took < 6, "start took %.1f s with START_TIMEOUT=1" % took
        silent = r.backends["silent"]
        assert silent.status != "ok" and "Timeout" in silent.error, (silent.status, silent.error)
        silent.proc.wait(timeout=5)              # killed, not left running
        assert r.backends["stub"].status == "ok"  # one silent backend costs only itself
    finally:
        _kill_all(r)


def test_call_timeout_returns_a_tool_error_and_frees_the_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "CALL_TIMEOUT", 1.0)
    r = tg.Router(str(_config(tmp_path, {"mute": MUTE_ON_CALL_SERVER})))
    try:
        r.start()
        assert r.backends["mute"].status == "ok"
        for attempt in (1, 2):   # the second call proves _lock was released
            finished, took, text = _run_with_watchdog(
                lambda: r.handle("call_tool", {"server": "mute", "tool": "wait"}))
            assert finished, "call %d still blocked after 15 s with CALL_TIMEOUT=1" % attempt
            assert took < 6, "call %d took %.1f s" % (attempt, took)
            assert "did not answer within" in text and "TOOL_GUARDIAN_CALL_TIMEOUT" in text, text
        rows = [json.loads(x) for x in pathlib.Path(tg.CALL_LOG).read_text(encoding="utf-8").splitlines()]
        assert [row["ok"] for row in rows if row.get("tool") == "call_tool"] == [False, False]
    finally:
        _kill_all(r)


# ------------------------------------------------------- 2 stalled sessions --

def _session(sid, *tools):
    rows = [{"kind": "session", "event": "start", "session": sid, "ok": True}]
    rows += [{"kind": "router", "tool": t, "session": sid, "ok": True} for t in tools]
    return rows


def test_summarize_search_only_session_is_stalled():
    rows = (_session("s_search", "search_capabilities", "search_capabilities")
            + _session("s_skills", "list_skills", "read_skill")
            + _session("s_spill", "retrieve_spill", "list_groups_with_costs")
            + _session("s_called", "search_capabilities", "call_tool"))
    s = tg_state.summarize(rows)
    assert s["stalled_discovery"] == ["s_search", "s_skills", "s_spill"]
    assert s["call_tool_calls"] == 1
    assert s["discovery_calls"] == 7
    assert s["silent_sessions"] == []


def test_summarize_other_router_tools_are_neither_discovery_nor_invoked():
    s = tg_state.summarize(_session("s_act", "activate_group"))
    assert s["stalled_discovery"] == [] and s["silent_sessions"] == []


# ------------------------------------------------------------- 3 spill store --

def test_spill_writes_the_sidecar_before_the_text(tmp_path, monkeypatch):
    order = []
    real = os.replace

    def spy(src, dst):
        order.append(pathlib.Path(dst).suffix)
        return real(src, dst)

    monkeypatch.setattr(tg_spill.os, "replace", spy)
    tg_spill.SpillStore(tmp_path).save("hello", tool="t", rule="r")
    assert order == [".json", ".txt"]


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_spill_rebuilds_a_missing_or_invalid_sidecar(tmp_path, damage):
    store = tg_spill.SpillStore(tmp_path)
    first = store.save("hello world", tool="t", rule="r")
    sidecar = tmp_path / (first["id"] + ".json")
    if damage == "missing":
        sidecar.unlink()
    else:
        sidecar.write_text("{not json", encoding="utf-8")
    again = store.save("hello world", tool="t", rule="r")
    assert again == first
    assert json.loads(sidecar.read_text(encoding="utf-8"))["chars"] == len("hello world")


def test_spill_prune_survives_a_concurrent_delete(tmp_path, monkeypatch):
    store = tg_spill.SpillStore(tmp_path, keep=2)
    for i in range(4):
        (tmp_path / ("sp_%012x.txt" % i)).write_text("x", encoding="utf-8")
    real_glob = pathlib.Path.glob

    def racing_glob(self, pattern):
        found = list(real_glob(self, pattern))
        found[0].unlink()   # another process pruned it between listing and stat()
        return found

    monkeypatch.setattr(pathlib.Path, "glob", racing_glob)
    removed = store.prune()
    assert removed >= 1 and len(list(real_glob(tmp_path, "sp_*.txt"))) == 2


# --------------------------------------------------- 4 archive failures -----

def test_archive_failure_is_counted(tmp_path):
    class BrokenStore:
        def save(self, *a, **k):
            raise OSError("disk full")

    r = tg.Router(str(_config(tmp_path, {})))
    r._spill = BrokenStore()
    text, meta = r.shape(BIG, tool="echo")
    assert meta["rule"] == "archive_failed"
    assert r.stats["archive_failures"] == 1


# ------------------------------------------------ 5 call_tool without server --

def test_call_tool_resolves_server_dot_tool_without_server(tmp_path):
    r = tg.Router(str(_config(tmp_path, {"stub": STUB_SERVER})))
    try:
        r.start()
        assert "echo: hi" in r.handle("call_tool", {"tool": "stub.echo", "args": {"text": "hi"}})
        assert "echo: yo" in r.handle("call_tool", {"tool": "echo", "args": {"text": "yo"}})
    finally:
        _kill_all(r)


def test_call_tool_without_server_is_clear_when_ambiguous(tmp_path):
    r = tg.Router(str(_config(tmp_path, {"one": STUB_SERVER, "two": STUB_SERVER})))
    try:
        r.start()
        out = r.handle("call_tool", {"tool": "echo", "args": {"text": "hi"}})
        assert "ambiguous" in out and "one.echo" in out and "two.echo" in out, out
        assert "echo: hi" in r.handle("call_tool", {"tool": "two.echo", "args": {"text": "hi"}})
        missing = r.handle("call_tool", {"tool": "nope"})
        assert "no tool named 'nope'" in missing, missing
    finally:
        _kill_all(r)


# ------------------------------------------------- 6 retrieve_spill past end --

@pytest.mark.skipif(tg.tg_ladder is None or tg.tg_spill is None, reason="ladder/spill modules missing")
def test_retrieve_spill_past_the_end_says_so(tmp_path):
    r = tg.Router(str(_config(tmp_path, {"stub": STUB_SERVER})),
                  options={"spillDir": str(tmp_path / "spill")})
    try:
        r.start()
        out = r.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": BIG}})
        sid = re.search(r"sp_[0-9a-f]{12}", out).group(0)
        past = r.handle("retrieve_spill", {"id": sid, "start_line": 99999})
        assert re.search(r"no lines at start_line 99999; total_lines \d+", past), past
        assert "lines 99999-99999" not in past
    finally:
        _kill_all(r)


# ------------------------------------------------------- 7 .env tunables ----

def test_env_file_tunables_are_honoured(tmp_path, monkeypatch, capsys):
    keys = ("TOOL_GUARDIAN_CALL_TIMEOUT", "TOOL_GUARDIAN_START_TIMEOUT", "TOOL_GUARDIAN_CONFIG")
    for k in keys:
        monkeypatch.setenv(k, "x")   # registers the original (absent) value for undo
        monkeypatch.delenv(k)
    for name in ("CALL_TIMEOUT", "START_TIMEOUT", "CHARS_PER_TOKEN", "LOG_PATH", "SKILLS_DIRS",
                 "LADDER_ON", "RAW_RESULTS", "CALL_LOG"):
        monkeypatch.setattr(tg, name, getattr(tg, name))
    cfg = _config(tmp_path, {})
    (tmp_path / ".env").write_text("TOOL_GUARDIAN_CALL_TIMEOUT=7\nTOOL_GUARDIAN_START_TIMEOUT=3\n",
                                   encoding="utf-8")
    try:
        tg.main(["--selftest", "--config", str(cfg)])
        capsys.readouterr()
        assert tg.CALL_TIMEOUT == 7.0 and tg.START_TIMEOUT == 3.0
    finally:
        for k in keys:
            os.environ.pop(k, None)


# ---------------------------------------------------------- 8 --selftest ----

def test_selftest_prints_per_server_cost_and_fails_on_any_dead_backend(tmp_path, capsys):
    cfg = _config(tmp_path, {"stub": STUB_SERVER,
                             "broken": {"command": str(tmp_path / ("missing_" + "x" * 200))}})
    rc = tg.selftest(str(cfg))
    out = capsys.readouterr().out
    assert rc != 0, "selftest exited 0 with a dead backend"
    assert re.search(r"^\[stub\] 2 tools, ~\d+ tokens$", out, re.M), out


def test_catalogue_shows_160_chars_of_a_backend_error(tmp_path):
    r = tg.Router(str(_config(tmp_path, {"broken": {"command": str(tmp_path / ("missing_" + "x" * 200))}})))
    r.start()
    err = r.backends["broken"].error
    assert len(err) > 160
    assert ("[broken] %s: %s" % (r.backends["broken"].status, err[:160])) in r.catalogue().splitlines()
