"""P110 S3 (A5) acceptance -- terse mode for small context windows. Written by the O5 overseer; DO NOT EDIT.

Why: the README's "What it does not do" says "There is no terse mode for the descriptions yet". With the
`small` preset the router's own `tools/list` still measured 1,090 tokens on 0.8.1 (2026-10-30, the same
end-to-end measure as tests/test_tools_list_budget.py): the preset trims the catalogue, not the router's six
descriptions. On an 8k-context local model that is ~13 % of the window before the first message.

Contract (plan P110 rev 1, decision 8 + C6, with the O5 deviation recorded in the pickup: the plan's "<= 550
target" is below the payload's fixed floor -- titles + annotations + schemas alone measure ~690 tokens -- so the
measured ceiling is 700 and at most 70 % of the default payload):
- TOOL_GUARDIAN_PRESET=small, or TOOL_GUARDIAN_TERSE=1 on its own, switches the router's tools to a terse set.
  TOOL_GUARDIAN_TERSE=0 or unset leaves the default payload byte-identical.
- Terse payload <= 700 tokens (chars / 3.5, serialised exactly as served) and <= 0.70 x the default payload.
- The terse set keeps list_capabilities, search_capabilities, describe_tool, call_tool and retrieve_spill (it MAY
  drop list_groups_with_costs); same names keep the same inputSchema `required` lists and properties; every tool
  keeps its MCP annotations with the same hints as the default; each description is non-empty and <= 160 chars.
- With a live backend, the terse list_capabilities description still names the server (docs/adr/0002).
- tests/test_tools_list_budget.py defines BUDGET_SMALL (an int <= 700) and the CHANGELOG records it with the
  measurement; the README no longer says "There is no terse mode".
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHARS_PER_TOKEN = 3.5
CORE = {"list_capabilities", "search_capabilities", "describe_tool", "call_tool", "retrieve_spill"}


def served_tools(tmp_path, env=None, servers=None):
    """The router's tools/list exactly as a client receives it, over stdio."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": servers or {}}), encoding="utf-8")
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "p110-a5", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    base = {k: v for k, v in os.environ.items()
            if k not in ("TOOL_GUARDIAN_PRESET", "TOOL_GUARDIAN_TERSE")}
    base.update(TOOL_GUARDIAN_HOME=str(tmp_path), TOOL_GUARDIAN_CALL_LOG="")
    base.update(env or {})
    run = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                         input="".join(json.dumps(m) + "\n" for m in requests),
                         capture_output=True, text=True, timeout=120, env=base, cwd=str(tmp_path))
    replies = {m["id"]: m for m in map(json.loads, run.stdout.splitlines()) if "id" in m}
    assert 2 in replies, run.stderr[-800:]
    return replies[2]["result"]["tools"]


def tokens(tools):
    return round(len(json.dumps(tools)) / CHARS_PER_TOKEN)


def by_name(tools):
    return {t["name"]: t for t in tools}


def hints(tool):
    ann = tool.get("annotations") or {}
    return {k: ann.get(k) for k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")}


def test_default_payload_is_unchanged_when_terse_is_off(tmp_path):
    unset = served_tools(tmp_path / "a")
    off = served_tools(tmp_path / "b", {"TOOL_GUARDIAN_TERSE": "0"})
    assert json.dumps(unset) == json.dumps(off)
    assert tokens(unset) <= 1100


def test_small_preset_fits_the_terse_budget(tmp_path):
    default = tokens(served_tools(tmp_path / "a"))
    small = tokens(served_tools(tmp_path / "b", {"TOOL_GUARDIAN_PRESET": "small"}))
    assert small <= 700, "small preset tools/list is %d tokens (ceiling 700)" % small
    assert small <= 0.70 * default, "small %d vs default %d: not 30 %% cheaper" % (small, default)


def test_terse_env_alone_gives_the_same_tools_as_the_small_preset(tmp_path):
    a = served_tools(tmp_path / "a", {"TOOL_GUARDIAN_TERSE": "1"})
    b = served_tools(tmp_path / "b", {"TOOL_GUARDIAN_PRESET": "small"})
    assert json.dumps(a) == json.dumps(b)
    assert tokens(a) <= 700


def test_terse_keeps_the_core_tools_and_their_contracts(tmp_path):
    default = by_name(served_tools(tmp_path / "a"))
    terse = by_name(served_tools(tmp_path / "b", {"TOOL_GUARDIAN_TERSE": "1"}))
    assert CORE <= set(terse), sorted(terse)
    assert set(terse) <= set(default), sorted(set(terse) - set(default))
    for name, tool in terse.items():
        want = default[name]["inputSchema"]
        got = tool["inputSchema"]
        assert sorted(got.get("required") or []) == sorted(want.get("required") or []), name
        assert set(got.get("properties") or {}) == set(want.get("properties") or {}), name
        assert hints(tool) == hints(default[name]), name
        assert tool.get("annotations"), name


def test_terse_descriptions_are_short_and_present(tmp_path):
    for tool in served_tools(tmp_path, {"TOOL_GUARDIAN_TERSE": "1"}):
        desc = tool.get("description") or ""
        assert desc.strip(), tool["name"]
        assert len(desc) <= 160, "%s: %d chars" % (tool["name"], len(desc))


def test_terse_catalogue_still_names_a_live_server(tmp_path):
    backend = [sys.executable, "-c",
               "import sys; sys.path.insert(0, %r); import tg_prove; sys.exit(tg_prove.run_backend())"
               % str(ROOT)]
    servers = {"notes": {"command": backend[0], "args": backend[1:]}}
    terse = by_name(served_tools(tmp_path, {"TOOL_GUARDIAN_TERSE": "1"}, servers))
    assert "notes" in terse["list_capabilities"]["description"]


def test_budget_small_is_written_down():
    src = (ROOT / "tests" / "test_tools_list_budget.py").read_text(encoding="utf-8")
    m = re.search(r"^BUDGET_SMALL\s*=\s*(\d+)", src, re.M)
    assert m and int(m.group(1)) <= 700, "tests/test_tools_list_budget.py needs BUDGET_SMALL <= 700"
    log = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    top = log.split("\n## ", 2)[1] if "\n## " in log else log
    assert "BUDGET_SMALL" in top and re.search(r"\b\d{3}\b", top), "CHANGELOG top entry must record BUDGET_SMALL"


def test_readme_no_longer_says_there_is_no_terse_mode():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "There is no terse mode" not in readme
    assert "TOOL_GUARDIAN_TERSE" in readme
