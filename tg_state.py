#!/usr/bin/env python3
"""tg_state.py -- tool-guardian's cross-session state and call-log reader.

Three jobs, all stdlib-only and side-effect free on import:

* remember which tool groups each session activated in ``state.json`` so the
  next session can *offer* to restore them (it never activates them itself),
* summarise ``calls.jsonl`` (router calls plus shell calls that bypassed the
  router, with the exact ``call_tool(...)`` to use instead),
* act as a Claude Code ``PreToolUse`` hook -- the only way a plain MCP server
  can learn about the client's own shell calls.

Every network/filesystem location is a parameter, so the selftest never
touches the real ``~/.tool-guardian``.  Standard library only, Python 3.9+.
"""

from __future__ import annotations
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

STATE_VERSION = 1
DEFAULT_SHELL_TOOLS = ("Bash", "PowerShell")

DISCOVERY_TOOLS = ("list_capabilities", "describe_tool")
#: probes of the call log that only *look* tools up instead of calling them
EMPTY_STATE = {"version": STATE_VERSION, "sessions": []}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _stamp(now=None):
    """Return *now* if given, else the current local ISO second."""
    if isinstance(now, str) and now:
        return now
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _dict_rows(rows):
    """Yield only the dict entries of *rows* (logs may hold anything)."""
    for row in rows or []:
        if isinstance(row, dict):
            yield row


def _text(value):
    """Return *value* when it is a non-empty string, else ""."""
    return value if isinstance(value, str) else ""


def _names(value):
    """Sorted list of the non-empty string tool names in *value*."""
    if isinstance(value, (list, tuple)):
        return sorted({n for n in value if isinstance(n, str) and n})
    return []


# --------------------------------------------------------------------------
# state.json
# --------------------------------------------------------------------------

def default_state_path(env=None):
    """Where state.json lives.  ``TOOL_GUARDIAN_STATE`` wins verbatim; "" disables."""
    if env is None:
        env = os.environ
    if "TOOL_GUARDIAN_STATE" in env:
        return env["TOOL_GUARDIAN_STATE"]
    return str(Path.home() / ".tool-guardian" / "state.json")


def new_session_id(clock=time.time, pid=None):
    """A sortable, human-readable session id: ``s<YYYYmmddHHMMSS>-<pid>``."""
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(clock()))
    return f"s{stamp}-{pid if pid is not None else os.getpid()}"


def load_state(path):
    """Read state.json.  Missing/broken/disabled all give the empty state; never raises."""
    empty = {"version": STATE_VERSION, "sessions": []}
    if not path:
        return empty
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        return empty
    return data


def save_state(path, state):
    """Atomically write *state* as JSON.  No temp file is ever left behind."""
    if not path:
        return False
    directory = os.path.dirname(path) or "."
    handle = None
    tmp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        handle, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            handle = None
            json.dump(state, out, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if handle is not None:
            os.close(handle)
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def _prune(sessions, keep):
    """Keep the *keep* sessions with the newest "updated", list order preserved."""
    if not isinstance(keep, int) or keep < 0:
        return sessions
    if len(sessions) <= keep:
        return sessions
    # newest first; on equal stamps the later list position wins
    ranked = sorted(enumerate(sessions), key=lambda pair: (_text(pair[1].get("updated")), pair[0]), reverse=True)
    winners = sorted(index for index, _ in ranked[:keep])
    return [sessions[index] for index in winners]


def record_activation(path, session_id, group, now=None, keep=10):
    """Note that *session_id* has *group* active; return the state written to disk."""
    stamp = _stamp(now)
    state = load_state(path)
    sessions = state["sessions"]
    entry = None
    for candidate in sessions:
        if isinstance(candidate, dict) and candidate.get("id") == session_id:
            entry = candidate
            break
    if entry is None:
        entry = {"id": session_id, "started": stamp, "updated": stamp, "active_groups": []}
        sessions.append(entry)
    if not isinstance(entry.get("active_groups"), list):
        entry["active_groups"] = []
    if group not in entry["active_groups"]:
        entry["active_groups"].append(group)
    entry["updated"] = stamp
    state["sessions"] = _prune(sessions, keep)
    save_state(path, state)
    return state


def last_session_groups(state, current_session=""):
    """The most recently updated *other* session that had groups active, or None."""
    best = None
    best_key = None
    for index, session in enumerate(_dict_rows(state.get("sessions") if isinstance(state, dict) else [])):
        groups = session.get("active_groups")
        if session.get("id") == current_session or not isinstance(groups, list) or not groups:
            continue
        key = (_text(session.get("updated")), index)
        if best_key is None or key > best_key:
            best_key = key
            best = session
    if best is None:
        return None
    return {"session": best.get("id"), "updated": best.get("updated"), "groups": list(best["active_groups"])}


def record_catalogue(path, servers_tools, now=None):
    """Remember the last advertised ``{server: [tool names]}``; return the new state."""
    state = load_state(path)
    servers = {}
    for server, names in (servers_tools or {}).items():
        servers[server] = _names(names)
    state["catalogue"] = {"updated": _stamp(now), "servers": {name: servers[name] for name in sorted(servers)}}
    save_state(path, state)
    return state


# --------------------------------------------------------------------------
# calls.jsonl
# --------------------------------------------------------------------------

def read_calls(path, max_bytes=5_000_000):
    """The call log, oldest first.  Junk lines are skipped, never fatal."""
    if not path:
        return []
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    text = ""
    try:
        if size > max_bytes > 0:
            with open(path, "rb") as handle:
                handle.seek(size - max_bytes)
                text = handle.read().decode("utf-8", "replace")
            # the first line here is a fragment cut in half by the seek
            text = text.split("\n", 1)[1] if "\n" in text else ""
        else:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
    except OSError:
        return []
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def last_session(rows):
    """The session id to attribute the log to: last start row, else last session seen."""
    started = ""
    fallback = ""
    for row in _dict_rows(rows):
        session = _text(row.get("session"))
        if not session:
            continue
        fallback = session
        if row.get("kind") == "session":
            started = session
    return started or fallback


def _scope_rows(rows, session, since):
    """Apply the session / since / all filter described by the contract."""
    if session:
        return [r for r in _dict_rows(rows) if r.get("session") == session]
    if since:
        scoped = []
        for row in _dict_rows(rows):
            stamp = _text(row.get("ts"))
            if stamp and stamp >= since:
                scoped.append(row)
        return scoped
    return list(_dict_rows(rows))


def summarize(rows, session=None, since=None):
    """Count router calls and router bypasses in the requested slice of the log."""
    scope = session or ("since " + since if since else "all")
    rows = _scope_rows(rows, session, since)

    router_calls = 0
    router_errors = 0
    discovery_calls = 0
    call_tool_calls = 0
    bypasses = 0
    targets = {}
    sessions = set()
    started = set()
    harnesses = set()
    per_session = {}

    for row in rows:
        kind = row.get("kind")
        tool = _text(row.get("tool"))
        sid = _text(row.get("session"))
        if sid:
            sessions.add(sid)
        if kind == "router":
            router_calls += 1
            if row.get("ok") is False:
                router_errors += 1
            if tool in DISCOVERY_TOOLS:
                discovery_calls += 1
            if tool == "call_tool":
                call_tool_calls += 1
            if sid:
                counts = per_session.setdefault(sid, [0, 0])
                counts[0 if tool in DISCOVERY_TOOLS else 1] += 1
        elif kind == "bypass":
            bypasses += 1
            key = (_text(row.get("server")), _text(row.get("target")))
            entry = targets.setdefault(key, {"count": 0, "shell_tools": set(), "modes": set()})
            entry["count"] += 1
            if tool:
                entry["shell_tools"].add(tool)
            mode = _text(row.get("mode"))
            if mode:
                entry["modes"].add(mode)
        elif kind == "session":
            if sid:
                started.add(sid)
            harness = _text(row.get("harness"))
            if harness:
                harnesses.add(harness)

    silent_sessions = sorted(
        sid for sid in started
        if sum(per_session.get(sid, (0, 0))) == 0
    )
    stalled = sorted(sid for sid, (discovery, invoked) in per_session.items() if discovery > 0 and invoked == 0)

    return {
        "scope": scope,
        "router_calls": router_calls,
        "router_errors": router_errors,
        "discovery_calls": discovery_calls,
        "call_tool_calls": call_tool_calls,
        "bypasses": bypasses,
        "bypass_by_target": [
            {
                "server": server,
                "tool": tool_name,
                "count": entry["count"],
                "shell_tools": sorted(entry["shell_tools"]),
                "modes": sorted(entry["modes"]),
            }
            for (server, tool_name), entry in sorted(
                targets.items(), key=lambda item: (-item[1]["count"], item[0][0], item[0][1])
            )
        ],
        "sessions": len(sessions),
        "silent_sessions": silent_sessions,
        "stalled_discovery": stalled,
        "harnesses": sorted(harnesses),
    }


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 'es'}"


def render_bypass_summary(summary):
    """Plain-text report: what bypassed the router, and the exact call to use instead."""
    summary = summary if isinstance(summary, dict) else {}
    scope = summary.get("scope", "all")
    lines = [
        f"tool-guardian ({scope}): {summary.get('router_calls', 0)} router calls, "
        + _plural(summary.get("bypasses", 0), "bypass")
    ]
    targets = summary.get("bypass_by_target") or []
    if not targets:
        lines.append("  No bypasses recorded.")
    for entry in targets:
        name = entry.get("tool", "")
        server = entry.get("server", "")
        shells = ", ".join(entry.get("shell_tools") or []) or "a shell"
        modes = ", ".join(entry.get("modes") or []) or "unknown"
        lines.append(
            f"  {entry.get('count', 0)}x {shells} ran {name} (mode {modes})"
            f' -- next time: call_tool(server="{server}", tool="{name}", args={{...}})'
        )
    for sid in summary.get("silent_sessions") or []:
        lines.append(f"  session {sid} never called the router (the model may have worked around it)")
    for sid in summary.get("stalled_discovery") or []:
        lines.append(f"  session {sid} looked tools up but never called call_tool")
    harnesses = summary.get("harnesses") or []
    if "mcp" in harnesses or not harnesses:
        lines.append(
            "  Note: over plain MCP a server cannot see the client's own shell calls, so bypasses"
        )
        lines.append(
            "  only show up here when the Claude Code PreToolUse hook"
            " (tool-guardian --hook-pretooluse) is installed."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Claude Code PreToolUse hook
# --------------------------------------------------------------------------

def _whole_word(name):
    """Regex matching *name* as a standalone token, not a substring of a longer one."""
    return r"(^|[^A-Za-z0-9_])" + re.escape(name) + r"([^A-Za-z0-9_]|$)"


def hook_check(payload, catalogue, rules=(), min_name_length=8, shell_tools=DEFAULT_SHELL_TOOLS):
    """Is this shell command poking at a router tool?  Return {"server", "tool"} or None."""
    if not isinstance(payload, dict) or payload.get("tool_name") not in (shell_tools or ()):
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command:
        return None

    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        try:
            found = re.search(_text(rule.get("pattern")), command, re.IGNORECASE) is not None
        except re.error:
            continue
        if found:
            return {"server": rule.get("server"), "tool": rule.get("tool")}

    servers = catalogue if isinstance(catalogue, dict) else {}
    for server in sorted(servers):
        for name in servers.get(server) or []:
            if not isinstance(name, str) or len(name) < min_name_length:
                continue
            try:
                found = re.search(_whole_word(name), command) is not None
            except re.error:
                continue
            if found:
                return {"server": server, "tool": name}
    return None


def _default_log_path():
    return os.environ.get("TOOL_GUARDIAN_CALL_LOG", str(Path.home() / ".tool-guardian" / "calls.jsonl"))


def _append_call_log(log_path, row):
    if not log_path:
        return
    directory = os.path.dirname(log_path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(row) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def run_hook(stdin_text, state_path=None, log_path=None, mode="log", rules=None, clock=None):
    """The PreToolUse entry point.  Returns ``(exit_code, stdout_text)``; never raises."""
    try:
        payload = json.loads(stdin_text) if stdin_text else None
        if not isinstance(payload, dict):
            return (0, "")
        catalogue = load_state(default_state_path() if state_path is None else state_path)
        catalogue = (catalogue.get("catalogue") or {}).get("servers")
        if not isinstance(catalogue, dict) or not catalogue:
            return (0, "")
        hit = hook_check(payload, catalogue, rules if rules is not None else ())
        if hit is None:
            return (0, "")

        if clock is not None:
            stamp = clock() if callable(clock) else _text(clock)
        else:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        session_id = _text(payload.get("session_id"))
        row = {
            "kind": "bypass",
            "harness": "claude-code-hook",
            "tool": payload.get("tool_name"),
            "server": hit.get("server"),
            "target": hit.get("tool"),
            "mode": mode,
            "ok": mode != "deny",
            "session": "cc-" + session_id if session_id else "",
            "ts": stamp,
        }
        # only the route/decision is recorded -- never the command itself
        _append_call_log(_default_log_path() if log_path is None else log_path, row)

        if mode == "deny":
            server = hit.get("server")
            name = hit.get("tool")
            reason = (
                f'"{name}" is a tool-guardian router tool.'
                f' Use call_tool(server="{server}", tool="{name}", args={{...}})'
                f' instead of the shell; describe_tool(server="{server}", tool="{name}")'
                " shows its arguments."
            )
            decision = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
            return (0, json.dumps(decision))
        return (0, "")
    except Exception:  # noqa: BLE001 -- a hook must never break the shell
        return (0, "")


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def _selftest_rows():
    return [
        {"kind": "session", "event": "start", "harness": "mcp", "session": "A", "ts": "2026-09-26T01:00:00"},
        {"kind": "router", "tool": "list_capabilities", "session": "A", "ok": True, "ts": "2026-09-26T01:01:00"},
        {"kind": "session", "event": "start", "harness": "dsh", "session": "C", "ts": "2026-09-26T02:00:00"},
        {"kind": "router", "tool": "list_capabilities", "session": "C", "ok": True, "ts": "2026-09-26T02:01:00"},
        {"kind": "router", "tool": "call_tool", "server": "n8n", "target": "wf", "session": "C", "ok": True,
         "ts": "2026-09-26T02:02:00"},
        {"kind": "bypass", "tool": "Bash", "server": "n8n", "target": "n8n_list_workflows", "mode": "nudge", "ok": True,
         "session": "C", "ts": "2026-09-26T02:03:00"},
        {"kind": "bypass", "tool": "PowerShell", "server": "n8n", "target": "n8n_list_workflows", "mode": "nudge",
         "ok": True, "session": "C", "ts": "2026-09-26T02:04:00"},
    ]


def _check_never_raises_on_bad_state(tmp):
    path = os.path.join(tmp, "broken.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    wrong = os.path.join(tmp, "wrong.json")
    with open(wrong, "w", encoding="utf-8") as handle:
        handle.write("[1, 2]")
    missing = os.path.join(tmp, "missing.json")
    return (load_state("") == {"version": 1, "sessions": []}
            and load_state(missing) == {"version": 1, "sessions": []}
            and load_state(path) == {"version": 1, "sessions": []}
            and load_state(wrong) == {"version": 1, "sessions": []})


def _check_atomic_save_leaves_no_temp(tmp):
    path = os.path.join(tmp, "nested", "state.json")
    ok = save_state(path, {"version": STATE_VERSION, "sessions": []})
    leftovers = [p.name for p in Path(path).parent.iterdir() if p.name != "state.json"]
    return ok is True and leftovers == [] and load_state(path)["version"] == STATE_VERSION \
        and save_state("", {}) is False


def _check_activation_dedup_order(tmp):
    path = os.path.join(tmp, "act.json")
    record_activation(path, "s1", "n8n", now="2026-09-26T06:00:00")
    record_activation(path, "s1", "files", now="2026-09-26T06:01:00")
    state = record_activation(path, "s1", "n8n", now="2026-09-26T06:02:00")
    session = state["sessions"][0]
    return (len(state["sessions"]) == 1 and session["active_groups"] == ["n8n", "files"]
            and session["started"] == "2026-09-26T06:00:00"
            and session["updated"] == "2026-09-26T06:02:00" and load_state(path) == state)


def _check_restore_offer_skips_current_session(tmp):
    path = os.path.join(tmp, "prune.json")
    for i in range(14):
        record_activation(path, f"s{i:02d}", "g", now=f"2026-09-26T06:{i:02d}:00", keep=10)
    ids = [s["id"] for s in load_state(path)["sessions"]]
    state = load_state(path)
    offer = last_session_groups(state, current_session="s13")
    return (len(ids) == 10 and "s13" in ids and "s00" not in ids and "s03" not in ids
            and offer == {"session": "s12", "updated": "2026-09-26T06:12:00", "groups": ["g"]}
            and last_session_groups({"version": 1, "sessions": []}) is None)


def _check_summary_counts_and_render_exact_call(_tmp):
    summary = summarize(_selftest_rows(), session="C")
    quiet = render_bypass_summary(summarize(_selftest_rows(), session="A"))
    text = render_bypass_summary(summary)
    top = summary["bypass_by_target"][0]
    return (summary["router_calls"] == 2 and summary["router_errors"] == 0
            and summary["discovery_calls"] == 1 and summary["call_tool_calls"] == 1
            and summary["bypasses"] == 2 and summary["scope"] == "C"
            and top["server"] == "n8n" and top["tool"] == "n8n_list_workflows" and top["count"] == 2
            and top["shell_tools"] == ["Bash", "PowerShell"]
            and 'call_tool(server="n8n", tool="n8n_list_workflows"' in text and "2x" in text
            and "2 bypasses" in text and "No bypasses" in quiet and "hook" in quiet.lower()
            and last_session(_selftest_rows()) == "C"
            and summarize(_selftest_rows())["sessions"] == 2)


def _check_hook_never_logs_command_text(tmp):
    state = os.path.join(tmp, "hook_state.json")
    log = os.path.join(tmp, "hook_calls.jsonl")
    record_catalogue(state, {"n8n": ["n8n_list_workflows"], "jobs": ["render_video"]}, now="2026-09-26T06:00:00")
    payload = json.dumps({"session_id": "abc", "tool_name": "Bash",
                          "tool_input": {"command": "node render_video.js --secret-token"}})
    code, out = run_hook(payload, state_path=state, log_path=log, mode="log")
    rows = read_calls(log)
    return (code == 0 and out == "" and len(rows) == 1 and rows[0]["kind"] == "bypass"
            and rows[0]["target"] == "render_video" and rows[0]["server"] == "jobs"
            and rows[0]["session"] == "cc-abc" and rows[0]["harness"] == "claude-code-hook"
            and rows[0]["ok"] is True
            and "render_video.js" not in json.dumps(rows[0])
            and "secret-token" not in json.dumps(rows[0])
            and hook_check({"tool_name": "Read", "tool_input": {"command": "render_video"}},
                           {"jobs": ["render_video"]}) is None
            and hook_check({"tool_name": "Bash", "tool_input": {"command": "render_videox"}},
                           {"jobs": ["render_video"]}) is None)


def _check_hook_deny_json_shape(tmp):
    state = os.path.join(tmp, "hook_state_deny.json")
    log = os.path.join(tmp, "hook_calls_deny.jsonl")
    record_catalogue(state, {"jobs": ["render_video"]}, now="2026-09-26T06:00:00")
    payload = json.dumps({"session_id": "abc", "tool_name": "Bash",
                          "tool_input": {"command": "node render_video.js"}})
    code, out = run_hook(payload, state_path=state, log_path=log, mode="deny")
    decision = json.loads(out)["hookSpecificOutput"]
    quiet = run_hook("not json", state_path=state, log_path=log, mode="deny")
    no_state = run_hook(payload, state_path=os.path.join(tmp, "absent.json"), log_path=log, mode="deny")
    rows = read_calls(log)
    return (code == 0 and decision["hookEventName"] == "PreToolUse"
            and decision["permissionDecision"] == "deny"
            and 'call_tool(server="jobs", tool="render_video"' in decision["permissionDecisionReason"]
            and "describe_tool" in decision["permissionDecisionReason"]
            and rows[0]["ok"] is False and len(rows) == 1
            and quiet == (0, "") and no_state == (0, ""))


def _check_default_state_path_is_home(_tmp):
    return default_state_path({}) == str(Path.home() / ".tool-guardian" / "state.json")


SELFTEST_CHECKS = (
    ("never_raises_on_bad_state", _check_never_raises_on_bad_state),
    ("atomic_save_leaves_no_temp", _check_atomic_save_leaves_no_temp),
    ("activation_dedup_order", _check_activation_dedup_order),
    ("restore_offer_skips_current_session", _check_restore_offer_skips_current_session),
    ("summary_counts_and_render_exact_call", _check_summary_counts_and_render_exact_call),
    ("hook_never_logs_command_text", _check_hook_never_logs_command_text),
    ("hook_deny_json_shape", _check_hook_deny_json_shape),
    ("default_state_path_is_home", _check_default_state_path_is_home),
)


def selftest():
    """Run every named check in a temp dir.  Returns the process exit code."""
    passed = 0
    with tempfile.TemporaryDirectory(prefix="tg_state_selftest_") as tmp:
        root = Path(tmp).resolve()
        for name, check in SELFTEST_CHECKS:
            why = ""
            try:
                ok = check(str(root)) is True
            except Exception as exc:  # noqa: BLE001 -- report, never crash the selftest
                ok, why = False, f"{type(exc).__name__}: {exc}"
            print(f"  {'ok  ' if ok else 'FAIL'} {name}")
            if not ok and why:
                print(f"       {why}")
            passed += 1 if ok else 0
    total = len(SELFTEST_CHECKS)
    print(f"tg_state selftest: {total} checks, {passed} passed, {total - passed} failed")
    return 0 if total > 0 and passed == total else 1


USAGE = "usage: tg_state.py --selftest"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--selftest"]:
        return selftest()
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
