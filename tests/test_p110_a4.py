"""P110 S5 (A4) acceptance (rev 2, 2026-10-30) -- a counted usage report. Written by the O5 overseer; DO NOT EDIT.

Why: "what does it actually save me?" is the first question an outside user asks, and today the answer is a
README bench line. The call log (~/.tool-guardian/calls.jsonl, ON by default, counts only -- never argument values)
already holds every router call; this turns it into a report, counted not estimated. Idea from XERJ's `xerj gain`.

Contract (plan P110 rev 1, decision 7 + C5; field names pinned here by O5):
- `tg_usage.py` (new, stdlib only, in pyproject py-modules and package.json "files").
- At startup the router also records, in state.json's "catalogue" object, `"tokens": {server: tokens}` (each live
  server's full tools/list schema cost, the same est_tokens the --selftest saving line uses) and
  `"router_tokens": <int>` (the router's own tools/list cost, model_cost of the served tools). The existing
  "servers" / "updated" keys stay.
- `tg_usage.parse_since("7d" | "24h" | "") -> seconds` (int; "" -> 0 meaning no limit; anything else -> ValueError).
- `tg_usage.summarize(rows, catalogue_tokens, router_tokens, since="") -> dict`, rows = call-log dicts, `since`
  an ISO "YYYY-MM-DDTHH:MM:SS" (rows with ts < since are ignored; "" keeps all). Returns exactly:
    {"sessions": n kind=="session" rows,
     "router_calls": n kind=="router" rows,
     "errors": n kind=="router" rows with ok is False,
     "servers": {server: {"calls", "errors", "chars_in", "chars_out"}} for every non-empty server named by a router
                row (chars_in/out = sums of original_chars/final_chars, missing -> 0),
     "shaping_chars_saved": total chars_in - chars_out over router rows,
     "catalogue_tokens_avoided": sessions * max(0, sum(catalogue_tokens.values()) - router_tokens),
     "unused": sorted servers in catalogue_tokens with no kind=="router", tool=="call_tool" row naming them,
     "unused_catalogue_tokens": sum of catalogue_tokens of the unused servers}
- `tool-guardian --usage [--since 7d] [--json]`: reads TOOL_GUARDIAN_CALL_LOG and the state.json catalogue; without
  --since the whole log is summarised (since = ""); with it, since = now minus parse_since(value);
  `--json` prints summarize()'s dict as one JSON object; the text form names every server, the avoided catalogue
  tokens, and a line `unused servers: <names>` (or `unused servers: none`). An empty or absent log prints
  `no calls in window` and exits 0.
- The report never contains argument values (the log has none; the report must not invent them).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROWS = [
    {"kind": "session", "event": "start", "harness": "mcp", "ok": True, "ts": "2026-10-20T09:00:00"},
    {"kind": "session", "event": "start", "harness": "mcp", "ok": True, "ts": "2026-10-29T09:00:00"},
    {"kind": "session", "event": "start", "harness": "mcp", "ok": True, "ts": "2026-10-30T09:00:00"},
    {"kind": "router", "tool": "call_tool", "server": "files", "target": "read", "arg_keys": ["args", "server", "tool"],
     "ok": True, "original_chars": 5000, "final_chars": 1200, "ts": "2026-10-30T09:01:00"},
    {"kind": "router", "tool": "call_tool", "server": "files", "target": "read", "arg_keys": ["server", "tool"],
     "ok": False, "ts": "2026-10-30T09:02:00"},
    {"kind": "router", "tool": "call_tool", "server": "git", "target": "log", "ok": True,
     "original_chars": 300, "final_chars": 300, "ts": "2026-10-30T09:03:00"},
    {"kind": "router", "tool": "describe_tool", "server": "web", "target": "fetch", "ok": True,
     "original_chars": 80, "final_chars": 80, "ts": "2026-10-30T09:04:00"},
    {"kind": "router", "tool": "list_capabilities", "server": "", "ok": True,
     "original_chars": 900, "final_chars": 900, "ts": "2026-10-30T09:05:00"},
    {"kind": "router", "tool": "call_tool", "server": "files", "target": "read", "ok": True,
     "original_chars": 100, "final_chars": 100, "ts": "2026-10-01T00:00:00"},
]
CATALOGUE = {"files": 4000, "git": 3000, "web": 2500, "db": 1500}


@pytest.fixture
def u():
    import importlib
    import tg_usage
    return importlib.reload(tg_usage)


def test_module_is_stdlib_and_packaged():
    src = (ROOT / "tg_usage.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w]*)", src, re.M))
    # rev 2: sys.stdlib_module_names is Python 3.10+; the package supports 3.9 (CI caught it on PR #23).
    stdlib = set(getattr(sys, "stdlib_module_names", ())) or {
        "__future__", "argparse", "collections", "datetime", "json", "math", "os", "pathlib", "re", "sys", "time",
        "typing"}
    assert imports <= stdlib | {"tg_state", "tool_guardian"}, imports
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tg_usage"' in re.search(r"^py-modules = \[(.*?)\]", py, re.M).group(1)
    assert "tg_usage.py" in json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"]


def test_parse_since(u):
    assert u.parse_since("7d") == 7 * 86400 and u.parse_since("24h") == 86400 and u.parse_since("") == 0
    with pytest.raises(ValueError):
        u.parse_since("soon")


def test_summarize_exact(u):
    got = u.summarize(ROWS, CATALOGUE, 1000, since="2026-10-29T00:00:00")
    assert got == {
        "sessions": 2,
        "router_calls": 5,
        "errors": 1,
        "servers": {"files": {"calls": 2, "errors": 1, "chars_in": 5000, "chars_out": 1200},
                    "git": {"calls": 1, "errors": 0, "chars_in": 300, "chars_out": 300},
                    "web": {"calls": 1, "errors": 0, "chars_in": 80, "chars_out": 80}},
        "shaping_chars_saved": 3800,
        "catalogue_tokens_avoided": 2 * (11000 - 1000),
        "unused": ["db", "web"],
        "unused_catalogue_tokens": 4000,
    }


def test_since_empty_keeps_everything(u):
    got = u.summarize(ROWS, CATALOGUE, 1000)
    assert got["sessions"] == 3 and got["router_calls"] == 6 and got["servers"]["files"]["calls"] == 3


def test_router_bigger_than_catalogue_never_goes_negative(u):
    assert u.summarize(ROWS, {"files": 10}, 1000)["catalogue_tokens_avoided"] == 0


def _home(tmp_path, rows):
    log = tmp_path / "calls.jsonl"
    if rows is not None:
        log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"version": 1, "sessions": [], "catalogue": {
        "updated": "2026-10-30T08:00:00", "servers": {k: [] for k in CATALOGUE},
        "tokens": CATALOGUE, "router_tokens": 1000}}), encoding="utf-8")
    env = dict(os.environ, TOOL_GUARDIAN_CALL_LOG=str(log), TOOL_GUARDIAN_STATE=str(state),
               TOOL_GUARDIAN_HOME=str(tmp_path))
    return env


def run_usage(tmp_path, rows, *extra):
    return subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--usage", *extra],
                          capture_output=True, text=True, timeout=120, env=_home(tmp_path, rows), cwd=str(tmp_path))


def test_cli_json_is_the_summary(tmp_path, u):
    r = run_usage(tmp_path, ROWS, "--json")
    assert r.returncode == 0, r.stderr[-500:]
    assert json.loads(r.stdout) == u.summarize(ROWS, CATALOGUE, 1000)


def test_cli_text_names_servers_savings_and_unused(tmp_path):
    r = run_usage(tmp_path, ROWS)
    assert r.returncode == 0, r.stderr[-500:]
    for name in ("files", "git", "web"):
        assert name in r.stdout
    assert "unused servers: db" in r.stdout
    assert "30000" in r.stdout.replace(",", "")   # 3 sessions x (11000 - 1000) catalogue tokens avoided


def test_cli_empty_log(tmp_path):
    for rows in (None, []):
        r = run_usage(tmp_path, rows)
        assert r.returncode == 0 and "no calls in window" in r.stdout


def test_report_never_prints_argument_values(tmp_path):
    rows = ROWS + [{"kind": "router", "tool": "call_tool", "server": "files", "target": "read",
                    "arg_keys": ["path"], "ok": True, "ts": "2026-10-30T10:00:00", "secret_value_xyz": "nope"}]
    r = run_usage(tmp_path, rows)
    rj = run_usage(tmp_path, rows, "--json")
    assert "secret_value_xyz" not in r.stdout and "nope" not in r.stdout and "nope" not in rj.stdout


def test_startup_records_catalogue_tokens(tmp_path):
    backend = [sys.executable, "-c",
               "import sys; sys.path.insert(0, %r); import tg_prove; sys.exit(tg_prove.run_backend())" % str(ROOT)]
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"notes": {"command": backend[0], "args": backend[1:]}}}),
                   encoding="utf-8")
    state = tmp_path / "state.json"
    env = dict(os.environ, TOOL_GUARDIAN_STATE=str(state), TOOL_GUARDIAN_HOME=str(tmp_path),
               TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "calls.jsonl"))
    r = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg), "--selftest"],
                       capture_output=True, text=True, timeout=180, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
    cat = json.loads(state.read_text(encoding="utf-8"))["catalogue"]
    assert cat["servers"]["notes"], cat
    assert isinstance(cat["tokens"]["notes"], int) and cat["tokens"]["notes"] > 0
    assert isinstance(cat["router_tokens"], int) and cat["router_tokens"] > 0
