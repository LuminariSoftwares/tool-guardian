"""The PreToolUse hook against Claude Code's own payloads, through the real CLI entry point.

Two inputs, both under tests/fixtures/claude_code/:
  - pretooluse_bash_docs.json: the PreToolUse Bash input example from
    https://code.claude.com/docs/en/hooks, verbatim;
  - pretooluse_bash_live_2.1.296.json: what Claude Code 2.1.296 actually piped to the hook in a
    live session on 2026-10-10 (ids and paths replaced, every key and value type kept).
    In that session the deny decision blocked the Bash call and Claude Code showed the reason.

The output is checked against the documented contract: exit 0, and either no stdout (the call
goes ahead) or one JSON object whose hookSpecificOutput carries hookEventName "PreToolUse",
permissionDecision "deny" and a permissionDecisionReason string.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "claude_code"
DOCS = json.loads((FIXTURES / "pretooluse_bash_docs.json").read_text(encoding="utf-8"))
LIVE = json.loads((FIXTURES / "pretooluse_bash_live_2.1.296.json").read_text(encoding="utf-8"))


def _catalogue(tmp_path, servers):
    state = tmp_path / "hook_state.json"
    state.write_text(json.dumps({"version": 1, "sessions": [], "catalogue": {"updated": "x", "servers": servers}}),
                     encoding="utf-8")
    return state


def _hook(tmp_path, payload, servers, *args):
    env = dict(os.environ, TOOL_GUARDIAN_STATE=str(_catalogue(tmp_path, servers)),
               TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "hook_calls.jsonl"))
    env.pop("TOOL_GUARDIAN_HOOK_MODE", None)
    return subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--hook-pretooluse", *args],
                          input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)


def _rows(tmp_path):
    path = tmp_path / "hook_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_documented_deny(stdout, server, tool):
    decision = json.loads(stdout)                     # exactly one JSON document on stdout
    assert set(decision) == {"hookSpecificOutput"}
    out = decision["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert isinstance(reason, str) and 'call_tool(server="%s", tool="%s"' % (server, tool) in reason


def test_fixtures_have_the_documented_common_fields():
    for payload in (DOCS, LIVE):
        for key in ("session_id", "transcript_path", "cwd", "permission_mode", "hook_event_name",
                    "tool_name", "tool_input", "tool_use_id"):
            assert key in payload, key
        assert payload["hook_event_name"] == "PreToolUse" and payload["tool_name"] == "Bash"
        assert isinstance(payload["tool_input"]["command"], str)


def test_docs_payload_unrelated_command_goes_ahead_silently(tmp_path):
    r = _hook(tmp_path, DOCS, {"jobs": ["render_video"]}, "--mode", "deny")
    assert r.returncode == 0 and r.stdout == "" and _rows(tmp_path) == []


def test_docs_payload_naming_a_router_tool_is_denied_with_the_documented_shape(tmp_path):
    payload = json.loads(json.dumps(DOCS))
    payload["tool_input"]["command"] = "python scripts/render_video --all"
    r = _hook(tmp_path, payload, {"jobs": ["render_video"]}, "--mode", "deny")
    assert r.returncode == 0
    _assert_documented_deny(r.stdout, "jobs", "render_video")
    row = _rows(tmp_path)[0]
    assert row["kind"] == "bypass" and row["session"] == "cc-abc123" and row["mode"] == "deny"


def test_docs_payload_log_mode_lets_the_call_run_and_logs_names_only(tmp_path):
    payload = json.loads(json.dumps(DOCS))
    payload["tool_input"]["command"] = "node render_video.js --secret-flag"
    r = _hook(tmp_path, payload, {"jobs": ["render_video"]})
    assert r.returncode == 0 and r.stdout == ""
    rows = _rows(tmp_path)
    assert len(rows) == 1 and rows[0]["target"] == "render_video" and rows[0]["ok"] is True
    assert "secret-flag" not in json.dumps(rows[0])          # never the command text


def test_live_claude_code_payload_is_denied(tmp_path):
    r = _hook(tmp_path, LIVE, {"probe": ["tg_live_probe_tool"]}, "--mode", "deny")
    assert r.returncode == 0
    _assert_documented_deny(r.stdout, "probe", "tg_live_probe_tool")
    row = _rows(tmp_path)[0]
    assert row["harness"] == "claude-code-hook" and row["session"] == "cc-" + LIVE["session_id"]


def test_mode_from_the_environment(tmp_path):
    env = dict(os.environ, TOOL_GUARDIAN_STATE=str(_catalogue(tmp_path, {"probe": ["tg_live_probe_tool"]})),
               TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "hook_calls.jsonl"), TOOL_GUARDIAN_HOOK_MODE="deny")
    r = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--hook-pretooluse"],
                       input=json.dumps(LIVE), capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0
    _assert_documented_deny(r.stdout, "probe", "tg_live_probe_tool")


def test_example_settings_wire_the_hook_for_both_shell_tools(tmp_path):
    settings = json.loads((ROOT / "examples" / "claude-code" / "settings.json").read_text(encoding="utf-8"))
    groups = settings["hooks"]["PreToolUse"]
    assert len(groups) == 1
    matcher = groups[0]["matcher"]
    for name in ("Bash", "PowerShell"):
        assert re.fullmatch(matcher, name)
    for name in ("Read", "Edit", "mcp__tool-guardian__call_tool"):
        assert not re.fullmatch(matcher, name)
    hook = groups[0]["hooks"][0]
    assert hook["type"] == "command"
    argv = hook["command"].split()
    assert argv[0] == "tool-guardian" and argv[1:] == ["--hook-pretooluse"]
    # the same arguments, run through this checkout instead of the installed script
    env = dict(os.environ, TOOL_GUARDIAN_STATE=str(_catalogue(tmp_path, {"probe": ["tg_live_probe_tool"]})),
               TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "hook_calls.jsonl"))
    env.pop("TOOL_GUARDIAN_HOOK_MODE", None)
    r = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), *argv[1:]],
                       input=json.dumps(LIVE), capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout == ""               # log mode: the call goes ahead
    assert _rows(tmp_path)[0]["mode"] == "log"


@pytest.mark.parametrize("stdin", ["", "not json", "[]", json.dumps({"tool_name": "Bash"})])
def test_broken_input_never_blocks(tmp_path, stdin):
    env = dict(os.environ, TOOL_GUARDIAN_STATE=str(_catalogue(tmp_path, {"probe": ["tg_live_probe_tool"]})),
               TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "hook_calls.jsonl"))
    r = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--hook-pretooluse", "--mode", "deny"],
                       input=stdin, capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout == ""
