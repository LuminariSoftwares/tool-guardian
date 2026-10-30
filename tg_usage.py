#!/usr/bin/env python3
"""tg_usage.py -- `tool-guardian --usage`: what the router saved, counted from the call log.

Called by: tool_guardian.main (--usage)

The call log (calls.jsonl) holds counts only -- server and tool names, key names, character
totals -- never an argument value, so this report cannot print one either. Two figures:

* catalogue tokens avoided = sessions x (what the live servers' full tools/list would cost -
  what the router's own tools/list costs). Both costs are recorded in state.json at router
  startup; the session count is the call log's `kind: session` rows.
* shaping chars saved = original_chars - final_chars summed over router rows (result shaping).

Standard library plus tg_state. MIT licensed.
"""
from __future__ import annotations

import re
import time

import tg_state

_SINCE = re.compile(r"^(\d+)([dh])$")


def parse_since(text) -> int:
    """'7d' / '24h' -> seconds; '' -> 0 (no limit); anything else raises ValueError."""
    text = (text or "").strip().lower()
    if not text:
        return 0
    m = _SINCE.match(text)
    if not m or int(m.group(1)) <= 0:
        raise ValueError("--since takes a number of days or hours such as 7d or 24h, not %r" % text)
    return int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)


def since_stamp(text, now=None) -> str:
    """The ISO local-time cut-off for `--since`, or '' when the whole log is wanted."""
    seconds = parse_since(text)
    if not seconds:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime((time.time() if now is None else now) - seconds))


def _int(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def summarize(rows, catalogue_tokens, router_tokens, since="") -> dict:
    """Count the call-log rows at or after `since` (ISO, '' keeps all). See the module docstring."""
    catalogue_tokens = {str(k): _int(v) for k, v in (catalogue_tokens or {}).items()}
    sessions = router_calls = errors = chars_in_total = chars_out_total = 0
    servers = {}
    called = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if since and not (isinstance(row.get("ts"), str) and row["ts"] >= since):
            continue
        kind = row.get("kind")
        if kind == "session":
            sessions += 1
            continue
        if kind != "router":
            continue
        router_calls += 1
        failed = row.get("ok") is False
        errors += 1 if failed else 0
        chars_in, chars_out = _int(row.get("original_chars")), _int(row.get("final_chars"))
        chars_in_total += chars_in
        chars_out_total += chars_out
        server = row.get("server")
        if not isinstance(server, str) or not server:
            continue
        entry = servers.setdefault(server, {"calls": 0, "errors": 0, "chars_in": 0, "chars_out": 0})
        entry["calls"] += 1
        entry["errors"] += 1 if failed else 0
        entry["chars_in"] += chars_in
        entry["chars_out"] += chars_out
        if row.get("tool") == "call_tool":
            called.add(server)
    unused = sorted(s for s in catalogue_tokens if s not in called)
    return {
        "sessions": sessions,
        "router_calls": router_calls,
        "errors": errors,
        "servers": servers,
        "shaping_chars_saved": chars_in_total - chars_out_total,
        "catalogue_tokens_avoided": sessions * max(0, sum(catalogue_tokens.values()) - _int(router_tokens)),
        "unused": unused,
        "unused_catalogue_tokens": sum(catalogue_tokens[s] for s in unused),
    }


def render(summary: dict, catalogue_tokens, window: str = "") -> str:
    """The human report. Token figures are chars/3.5 estimates (est_tokens), so they carry `~`."""
    if not summary["sessions"] and not summary["router_calls"]:
        return "no calls in window"
    lines = ["tool-guardian usage (%s): %d session(s), %d router call(s), %d error(s)"
             % (window or "whole call log", summary["sessions"], summary["router_calls"], summary["errors"])]
    servers = summary["servers"]
    width = max([len(s) for s in servers] or [0])
    for name in sorted(servers):
        s = servers[name]
        lines.append("  %s  calls %d  errors %d  chars in %s  out %s"
                     % (name.ljust(width), s["calls"], s["errors"], format(s["chars_in"], ","),
                        format(s["chars_out"], ",")))
    lines.append("result shaping saved %s chars" % format(summary["shaping_chars_saved"], ","))
    if not catalogue_tokens:
        lines.append("catalogue tokens avoided: unknown (state.json has no token counts yet; "
                     "they are recorded the next time the router starts)")
        return "\n".join(lines)
    lines.append("catalogue tokens avoided: ~%s" % format(summary["catalogue_tokens_avoided"], ","))
    unused = summary["unused"]
    lines.append("unused servers: %s" % (", ".join(unused) if unused else "none"))
    total = sum(catalogue_tokens.values())
    if unused:
        lines.append("dropping the unused servers would shrink the full catalogue by ~%s tokens (%d%% of ~%s)"
                     % (format(summary["unused_catalogue_tokens"], ","),
                        round(100.0 * summary["unused_catalogue_tokens"] / max(total, 1)), format(total, ",")))
    else:
        lines.append("dropping unused servers would save nothing: every server was called")
    return "\n".join(lines)


def catalogue_costs(state_path):
    """({server: tokens}, router_tokens) from state.json's catalogue; ({}, 0) when not recorded yet."""
    cat = tg_state.load_state(state_path).get("catalogue")
    cat = cat if isinstance(cat, dict) else {}
    tokens = cat.get("tokens")
    tokens = {str(k): _int(v) for k, v in tokens.items()} if isinstance(tokens, dict) else {}
    return tokens, _int(cat.get("router_tokens"))
