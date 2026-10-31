"""--since (--usage, --bypass-summary) and --since-hours must not swallow each other."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _stamp(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - hours_ago * 3600))


def _run(tmp_path, *args):
    rows = [{"kind": "router", "tool": "call_tool", "server": "files", "target": "read", "ok": True,
             "original_chars": 10, "final_chars": 10, "ts": _stamp(h)} for h in (30, 2)]
    rows.append({"kind": "session", "event": "start", "harness": "mcp", "ok": True, "ts": _stamp(2)})
    log = tmp_path / "calls.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    env = dict(os.environ, TOOL_GUARDIAN_CALL_LOG=str(log), TOOL_GUARDIAN_STATE=str(tmp_path / "state.json"),
               TOOL_GUARDIAN_HOME=str(tmp_path))
    return subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), *args], capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(tmp_path))


def _bypass_router_calls(r):
    assert r.returncode == 0, r.stderr[-500:]
    return int(re.search(r"(\d+) router calls", r.stdout).group(1))


def test_bypass_summary_since_bare_number_is_hours(tmp_path):
    assert _bypass_router_calls(_run(tmp_path, "--bypass-summary", "--since", "24")) == 1
    assert _bypass_router_calls(_run(tmp_path, "--bypass-summary", "--since", "48")) == 2


def test_bypass_summary_since_accepts_days_and_hours_units(tmp_path):
    assert _bypass_router_calls(_run(tmp_path, "--bypass-summary", "--since", "24h")) == 1
    assert _bypass_router_calls(_run(tmp_path, "--bypass-summary", "--since", "7d")) == 2


def test_bypass_summary_since_hours_unchanged(tmp_path):
    assert _bypass_router_calls(_run(tmp_path, "--bypass-summary", "--since-hours", "24")) == 1
    assert _bypass_router_calls(_run(tmp_path, "--bypass-summary", "--since-hours", "48")) == 2
    assert _bypass_router_calls(_run(tmp_path, "--bypass-summary")) == 1   # default 24 hours


def test_bypass_summary_bad_since_is_an_error(tmp_path):
    r = _run(tmp_path, "--bypass-summary", "--since", "soon")
    assert r.returncode == 2 and "--since" in r.stderr


def test_usage_since_days(tmp_path):
    r = _run(tmp_path, "--usage", "--since", "7d", "--json")
    assert r.returncode == 0, r.stderr[-500:]
    assert json.loads(r.stdout)["router_calls"] == 2
    r = _run(tmp_path, "--usage", "--since", "24h", "--json")
    assert json.loads(r.stdout)["router_calls"] == 1


def test_usage_ignores_since_hours(tmp_path):
    r = _run(tmp_path, "--usage", "--since-hours", "1", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["router_calls"] == 2   # whole log
