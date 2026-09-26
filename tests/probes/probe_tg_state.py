# @studio: QA | Overseer probe for tool-guardian's tg_state.py -- written from the CONTRACT, not from the module
# @kind: cli
# @called_by: human | tests/test_contract_probes.py
"""py probe_tg_state.py [dir-holding-tg_state.py]   -> last line: probe_tg_state: N checks, N passed, M failed"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "."))
import tg_state as S  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="probe_tg_state_"))


def _p(name):
    return str(TMP / name)


def _rows(*rows):
    return [dict(r) for r in rows]


# ---- state.json ---------------------------------------------------------------

def c_default_state_path_is_home_or_env():
    home = str(Path.home() / ".tool-guardian" / "state.json")
    return (S.default_state_path({}) == home
            and S.default_state_path({"TOOL_GUARDIAN_STATE": _p("x.json")}) == _p("x.json")
            and S.default_state_path({"TOOL_GUARDIAN_STATE": ""}) == "")


def c_session_id_is_injectable_and_stable():
    a = S.new_session_id(clock=lambda: 1790000000.0, pid=42)
    b = S.new_session_id(clock=lambda: 1790000000.0, pid=42)
    c = S.new_session_id(clock=lambda: 1790000001.0, pid=42)
    return isinstance(a, str) and a == b and a != c and "42" in a and len(a) <= 40 and " " not in a


def c_load_missing_or_broken_is_empty_state():
    broken = TMP / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    wrong = TMP / "wrong.json"
    wrong.write_text("[1, 2]", encoding="utf-8")
    empty = {"version": 1, "sessions": []}
    return (S.load_state(_p("nope.json")) == empty and S.load_state(str(broken)) == empty
            and S.load_state(str(wrong)) == empty and S.load_state("") == empty)


def c_save_is_atomic_and_disabled_path_writes_nothing():
    path = TMP / "sub" / "state.json"
    ok = S.save_state(str(path), {"version": 1, "sessions": []})
    leftovers = [p.name for p in path.parent.iterdir() if p.name != "state.json"]
    return ok is True and json.loads(path.read_text(encoding="utf-8"))["version"] == 1 \
        and leftovers == [] and S.save_state("", {"version": 1, "sessions": []}) is False


def c_record_activation_dedups_and_keeps_order():
    path = _p("act.json")
    S.record_activation(path, "s1", "n8n", now="2026-09-26T06:00:00")
    S.record_activation(path, "s1", "files", now="2026-09-26T06:01:00")
    st = S.record_activation(path, "s1", "n8n", now="2026-09-26T06:02:00")
    ses = [s for s in st["sessions"] if s["id"] == "s1"]
    on_disk = S.load_state(path)
    return (len(ses) == 1 and ses[0]["active_groups"] == ["n8n", "files"]
            and ses[0]["started"] == "2026-09-26T06:00:00" and ses[0]["updated"] == "2026-09-26T06:02:00"
            and on_disk == st)


def c_record_activation_trims_to_keep():
    path = _p("trim.json")
    for i in range(14):
        S.record_activation(path, "s%02d" % i, "g", now="2026-09-26T06:%02d:00" % i, keep=10)
    ids = [s["id"] for s in S.load_state(path)["sessions"]]
    return len(ids) == 10 and "s13" in ids and "s00" not in ids and "s03" not in ids


def c_last_session_groups_skips_current_and_empty():
    st = {"version": 1, "sessions": [
        {"id": "old", "started": "2026-09-25T10:00:00", "updated": "2026-09-25T11:00:00", "active_groups": ["files"]},
        {"id": "prev", "started": "2026-09-26T01:00:00", "updated": "2026-09-26T02:00:00", "active_groups": ["n8n", "db"]},
        {"id": "empty", "started": "2026-09-26T03:00:00", "updated": "2026-09-26T03:00:00", "active_groups": []},
        {"id": "now", "started": "2026-09-26T06:00:00", "updated": "2026-09-26T06:05:00", "active_groups": ["x"]},
    ]}
    got = S.last_session_groups(st, current_session="now")
    none = S.last_session_groups({"version": 1, "sessions": []}, current_session="now")
    return got == {"session": "prev", "updated": "2026-09-26T02:00:00", "groups": ["n8n", "db"]} and none is None


def c_record_catalogue_keeps_sessions():
    path = _p("cat.json")
    S.record_activation(path, "s1", "n8n", now="2026-09-26T06:00:00")
    st = S.record_catalogue(path, {"n8n": ["n8n_list_workflows"], "files": ["read_file"]}, now="2026-09-26T06:09:00")
    disk = S.load_state(path)
    return (disk["catalogue"] == {"updated": "2026-09-26T06:09:00",
                                  "servers": {"files": ["read_file"], "n8n": ["n8n_list_workflows"]}}
            and [s["id"] for s in disk["sessions"]] == ["s1"] and st == disk)


# ---- call-log summary --------------------------------------------------------

LOG = _rows(
    {"kind": "session", "event": "start", "harness": "mcp", "session": "A", "ts": "2026-09-26T01:00:00"},
    {"kind": "router", "tool": "list_capabilities", "session": "A", "ok": True, "ts": "2026-09-26T01:01:00"},
    {"kind": "session", "event": "start", "harness": "mcp", "session": "B", "ts": "2026-09-26T02:00:00"},
    {"kind": "session", "event": "start", "harness": "dsh", "session": "C", "ts": "2026-09-26T03:00:00"},
    {"kind": "router", "tool": "list_capabilities", "session": "C", "ok": True, "ts": "2026-09-26T03:01:00"},
    {"kind": "router", "tool": "call_tool", "server": "n8n", "target": "n8n_list", "session": "C", "ok": False, "ts": "2026-09-26T03:02:00"},
    {"kind": "router", "tool": "call_tool", "server": "n8n", "target": "n8n_list_workflows", "session": "C", "ok": True, "ts": "2026-09-26T03:03:00"},
    {"kind": "bypass", "tool": "bash", "server": "n8n", "target": "n8n_list_workflows", "mode": "nudge", "ok": True, "session": "C", "ts": "2026-09-26T03:04:00"},
    {"kind": "bypass", "tool": "pwsh", "server": "n8n", "target": "n8n_list_workflows", "mode": "nudge", "ok": True, "session": "C", "ts": "2026-09-26T03:05:00"},
    {"kind": "bypass", "tool": "bash", "server": "jobs", "target": "render_video", "mode": "deny", "ok": False, "session": "C", "ts": "2026-09-26T03:06:00"},
    {"kind": "ladder", "tool": "bash", "ok": True, "session": "C", "ts": "2026-09-26T03:07:00"},
    {"kind": "bypass", "tool": "Bash", "server": "jobs", "target": "render_video", "mode": "log", "ok": True, "harness": "claude-code-hook", "session": "cc-xyz", "ts": "2026-09-26T04:00:00"},
    {"kind": "router", "tool": "call_tool", "server": "old", "ok": True, "ts": "2026-09-20T01:00:00"},
)


def c_read_calls_skips_bad_lines_and_missing_file():
    path = TMP / "calls.jsonl"
    path.write_text('{"kind": "router", "tool": "a"}\nnot json\n\n[1]\n{"kind": "bypass", "tool": "bash"}\n',
                    encoding="utf-8")
    rows = S.read_calls(str(path))
    return [r["kind"] for r in rows] == ["router", "bypass"] and S.read_calls(_p("missing.jsonl")) == []


def c_last_session_prefers_session_start_rows():
    return S.last_session(LOG) == "C" and S.last_session([]) == "" \
        and S.last_session(_rows({"kind": "router", "session": "Z"})) == "Z"


def c_summary_one_session_counts():
    s = S.summarize(LOG, session="C")
    by = {(b["server"], b["tool"]): b for b in s["bypass_by_target"]}
    first = s["bypass_by_target"][0]
    return (s["router_calls"] == 3 and s["router_errors"] == 1 and s["bypasses"] == 3
            and s["call_tool_calls"] == 2 and s["discovery_calls"] == 1
            and by[("n8n", "n8n_list_workflows")]["count"] == 2
            and by[("n8n", "n8n_list_workflows")]["shell_tools"] == ["bash", "pwsh"]
            and by[("jobs", "render_video")]["modes"] == ["deny"]
            and first["tool"] == "n8n_list_workflows")


def c_summary_since_and_silent_sessions():
    s = S.summarize(LOG, since="2026-09-26T00:00:00")
    return (s["sessions"] == 4 and s["silent_sessions"] == ["B"] and s["stalled_discovery"] == ["A"]
            and s["bypasses"] == 4 and s["router_calls"] == 4)


def c_summary_all_includes_legacy_rows():
    s = S.summarize(LOG)
    return s["router_calls"] == 5 and s["bypasses"] == 4


def c_render_names_the_exact_call_and_counts():
    text = S.render_bypass_summary(S.summarize(LOG, session="C"))
    return ('call_tool(server="n8n", tool="n8n_list_workflows"' in text
            and "2x" in text and "bash" in text and "pwsh" in text and "3 bypass" in text)


def c_render_zero_bypass_and_mcp_caveat():
    quiet = S.render_bypass_summary(S.summarize(LOG, session="A"))
    silent = S.render_bypass_summary(S.summarize(LOG, session="B"))
    return ("no bypass" in quiet.lower() and "hook" in quiet.lower()
            and "never called the router" in silent)


# ---- Claude Code PreToolUse hook ----------------------------------------------

CAT = {"n8n": ["n8n_list_workflows", "run"], "jobs": ["render_video"]}


def c_hook_check_matches_whole_names_only():
    hit = S.hook_check({"tool_name": "Bash", "tool_input": {"command": "python x.py n8n_list_workflows --all"}}, CAT)
    short = S.hook_check({"tool_name": "Bash", "tool_input": {"command": "run it"}}, CAT)
    inside = S.hook_check({"tool_name": "Bash", "tool_input": {"command": "xn8n_list_workflowsx"}}, CAT)
    other = S.hook_check({"tool_name": "Read", "tool_input": {"file_path": "n8n_list_workflows"}}, CAT)
    rule = S.hook_check({"tool_name": "Bash", "tool_input": {"command": "curl localhost:5679/api"}}, CAT,
                        rules=[{"pattern": "localhost:5679", "server": "n8n", "tool": "n8n_list_workflows"}])
    return (hit == {"server": "n8n", "tool": "n8n_list_workflows"} and short is None and inside is None
            and other is None and rule == {"server": "n8n", "tool": "n8n_list_workflows"})


def _hook_env(name):
    state = TMP / ("hook_state_%s.json" % name)
    S.record_catalogue(str(state), CAT, now="2026-09-26T06:00:00")
    return str(state), str(TMP / ("hook_calls_%s.jsonl" % name))


def c_run_hook_log_mode_allows_and_logs():
    state, log = _hook_env("log")
    payload = json.dumps({"session_id": "abc", "tool_name": "Bash",
                          "tool_input": {"command": "node render_video.js"}})
    code, out = S.run_hook(payload, state_path=state, log_path=log, mode="log")
    rows = S.read_calls(log)
    return (code == 0 and out == "" and len(rows) == 1 and rows[0]["kind"] == "bypass"
            and rows[0]["harness"] == "claude-code-hook" and rows[0]["target"] == "render_video"
            and rows[0]["server"] == "jobs" and rows[0]["session"] == "cc-abc" and rows[0]["mode"] == "log"
            and "render_video.js" not in json.dumps(rows[0]))


def c_run_hook_deny_mode_emits_decision_json():
    state, log = _hook_env("deny")
    payload = json.dumps({"session_id": "abc", "tool_name": "Bash",
                          "tool_input": {"command": "node render_video.js"}})
    code, out = S.run_hook(payload, state_path=state, log_path=log, mode="deny")
    doc = json.loads(out)
    hso = doc["hookSpecificOutput"]
    return (code == 0 and hso["hookEventName"] == "PreToolUse" and hso["permissionDecision"] == "deny"
            and 'call_tool(server="jobs", tool="render_video"' in hso["permissionDecisionReason"]
            and S.read_calls(log)[0]["ok"] is False)


def c_run_hook_never_breaks_the_shell():
    state, log = _hook_env("safe")
    results = [
        S.run_hook("not json", state_path=state, log_path=log, mode="deny"),
        S.run_hook("", state_path=state, log_path=log, mode="deny"),
        S.run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}), state_path=state, log_path=log, mode="deny"),
        S.run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "node render_video.js"}}),
                   state_path=_p("no_state.json"), log_path=log, mode="deny"),
    ]
    return all(r == (0, "") for r in results) and S.read_calls(log) == []


CHECKS = [
    ("default_state_path_is_home_or_env", c_default_state_path_is_home_or_env),
    ("session_id_is_injectable_and_stable", c_session_id_is_injectable_and_stable),
    ("load_missing_or_broken_is_empty_state", c_load_missing_or_broken_is_empty_state),
    ("save_is_atomic_and_disabled_path_writes_nothing", c_save_is_atomic_and_disabled_path_writes_nothing),
    ("record_activation_dedups_and_keeps_order", c_record_activation_dedups_and_keeps_order),
    ("record_activation_trims_to_keep", c_record_activation_trims_to_keep),
    ("last_session_groups_skips_current_and_empty", c_last_session_groups_skips_current_and_empty),
    ("record_catalogue_keeps_sessions", c_record_catalogue_keeps_sessions),
    ("read_calls_skips_bad_lines_and_missing_file", c_read_calls_skips_bad_lines_and_missing_file),
    ("last_session_prefers_session_start_rows", c_last_session_prefers_session_start_rows),
    ("summary_one_session_counts", c_summary_one_session_counts),
    ("summary_since_and_silent_sessions", c_summary_since_and_silent_sessions),
    ("summary_all_includes_legacy_rows", c_summary_all_includes_legacy_rows),
    ("render_names_the_exact_call_and_counts", c_render_names_the_exact_call_and_counts),
    ("render_zero_bypass_and_mcp_caveat", c_render_zero_bypass_and_mcp_caveat),
    ("hook_check_matches_whole_names_only", c_hook_check_matches_whole_names_only),
    ("run_hook_log_mode_allows_and_logs", c_run_hook_log_mode_allows_and_logs),
    ("run_hook_deny_mode_emits_decision_json", c_run_hook_deny_mode_emits_decision_json),
    ("run_hook_never_breaks_the_shell", c_run_hook_never_breaks_the_shell),
]


def main():
    passed = 0
    try:
        for name, fn in CHECKS:
            try:
                ok, why = fn() is True, ""
            except Exception as exc:  # noqa: BLE001
                ok, why = False, " (%s: %s)" % (type(exc).__name__, exc)
            print("  %s %s%s" % ("ok  " if ok else "FAIL", name, why))
            passed += 1 if ok else 0
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    total = len(CHECKS)
    print("probe_tg_state: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
