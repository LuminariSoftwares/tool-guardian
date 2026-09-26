"""MVP wiring (2026-09-26): session ids in the call log, the MCP session-start row, the
catalogue in state.json for the Claude Code hook, `--bypass-summary`, `--hook-pretooluse`
and the update line in --selftest. The tg_state / tg_update contracts have their own probes;
these tests prove tool_guardian.py wires them up."""
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import tool_guardian as tg
from test_tool_guardian import STUB_SERVER

ROOT = Path(__file__).resolve().parent.parent


def _stub_config(tmp_path):
    stub = tmp_path / "stub_server.py"
    stub.write_text(STUB_SERVER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"stub": {"command": sys.executable, "args": [str(stub)]}}}),
                   encoding="utf-8")
    return cfg


def _rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_every_log_row_carries_the_router_session(tmp_path):
    r = tg.Router(str(_stub_config(tmp_path)))
    r.start()
    r.handle("list_capabilities", {})
    r.handle("call_tool", {"server": "stub", "tool": "echo", "args": {"text": "hi"}})
    rows = _rows(tg.CALL_LOG)
    assert r.session_id and len(rows) == 2 and all(row["session"] == r.session_id for row in rows)


def test_start_remembers_the_catalogue_for_the_hook(tmp_path, monkeypatch):
    r = tg.Router(str(_stub_config(tmp_path)))
    r.start()
    state = json.loads(Path(tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "echo" in state["catalogue"]["servers"]["stub"]


def test_mcp_initialize_logs_a_session_start_row(tmp_path, monkeypatch):
    r = tg.Router(str(_stub_config(tmp_path)))
    r.start()
    tg.ROUTER_TOOLS = tg.build_all_tools(r.backends)
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "claude-code"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_capabilities", "arguments": {}}},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(f) for f in frames) + "\n"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    tg.serve(r)
    rows = _rows(tg.CALL_LOG)
    assert rows[0]["kind"] == "session" and rows[0]["harness"] == "mcp" and rows[0]["client"] == "claude-code"
    assert rows[1]["kind"] == "router" and rows[1]["session"] == rows[0]["session"] == r.session_id


def test_bypass_summary_cli_reads_the_log(tmp_path, capsys):
    log = Path(tg.CALL_LOG)
    log.write_text("\n".join(json.dumps(r) for r in [
        {"kind": "session", "event": "start", "harness": "mcp", "session": "s1", "ts": "2026-09-26T01:00:00"},
        {"kind": "router", "tool": "call_tool", "session": "s1", "ok": True, "ts": "2026-09-26T01:01:00"},
        {"kind": "bypass", "tool": "Bash", "server": "n8n", "target": "n8n_list_workflows", "mode": "log",
         "ok": True, "harness": "claude-code-hook", "session": "cc-1", "ts": "2026-09-26T01:02:00"},
    ]) + "\n", encoding="utf-8")
    assert tg.main(["--bypass-summary", "--session", "all"]) == 0
    out = capsys.readouterr().out
    assert "1 bypass" in out and 'call_tool(server="n8n", tool="n8n_list_workflows"' in out and str(log) in out
    assert tg.main(["--bypass-summary", "--session", "last"]) == 0
    assert "(s1)" in capsys.readouterr().out


def _hook(tmp_path, stdin, *args):
    state = tmp_path / "hook_state.json"
    state.write_text(json.dumps({"version": 1, "sessions": [],
                                 "catalogue": {"updated": "x", "servers": {"jobs": ["render_video"]}}}),
                     encoding="utf-8")
    env = dict(__import__("os").environ, TOOL_GUARDIAN_STATE=str(state),
               TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "hook_calls.jsonl"))
    return subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--hook-pretooluse", *args],
                          input=stdin, capture_output=True, text=True, env=env, timeout=60)


def test_hook_cli_deny_mode_prints_the_decision(tmp_path):
    payload = json.dumps({"session_id": "abc", "tool_name": "Bash", "tool_input": {"command": "node render_video.js"}})
    r = _hook(tmp_path, payload, "--mode", "deny")
    decision = json.loads(r.stdout)["hookSpecificOutput"]
    assert r.returncode == 0 and decision["permissionDecision"] == "deny"
    row = _rows(tmp_path / "hook_calls.jsonl")[0]
    assert row["target"] == "render_video" and row["session"] == "cc-abc" and "render_video.js" not in json.dumps(row)


def test_hook_cli_never_breaks_the_shell(tmp_path):
    for stdin in ("", "garbage", json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})):
        r = _hook(tmp_path, stdin, "--mode", "deny")
        assert r.returncode == 0 and r.stdout == ""
    assert not (tmp_path / "hook_calls.jsonl").exists()


def test_selftest_ends_with_the_update_line(tmp_path, capsys):
    tg.selftest(str(_stub_config(tmp_path)))
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("update:") and "check off" in last   # conftest opts out: no network


@pytest.mark.skipif(shutil.which("node") is None or not (ROOT / "node_modules" / "@deepseek-ai" / "schemastery").exists(),
                    reason="node or the bundle's node_modules is not installed")
@pytest.mark.parametrize("script,name,minimum", [("probe_dsh_mvp.mjs", "probe_dsh_mvp", 21),
                                                 ("dsh_smoke.mjs", "dsh-tool-guardian smoke", 42),
                                                 ("probe_setup.mjs", "probe_setup", 9)])
def test_dsh_bundle_probes(script, name, minimum):
    import os
    # The probes isolate themselves; the autouse fixture's spill/state/log env vars would override their temp dirs.
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOOL_GUARDIAN_")}
    r = subprocess.run(["node", str(ROOT / "tests" / script)], cwd=str(ROOT), capture_output=True, text=True,
                       timeout=300, env=env)
    import re
    m = re.search(r"%s: (\d+) checks, (\d+) passed, (\d+) failed" % re.escape(name), r.stdout)
    assert m, r.stdout[-1500:] + r.stderr[-800:]
    total, passed, failed = (int(x) for x in m.groups())
    assert total >= minimum and failed == 0 and r.returncode == 0, r.stdout[-1500:]
