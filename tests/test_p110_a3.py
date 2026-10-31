"""P110 S2 (A3) acceptance -- a per-tool safety policy with a confirm-token gate. Written by O5; DO NOT EDIT.

Why: call_tool runs whatever tool the model names. A3 lets the user deny tools, ask before others, or make a session
read-only. The universal gate is a confirm token: the MODEL confirms, not the user (A3b adds the human gate later).

Contract (plan P110 decision 6 + C4 without A3b; details pinned by O5 2026-10-31):
- Policy file: TOOL_GUARDIAN_POLICY, else ~/.tool-guardian/policy.json, read when the Router starts:
  {"readOnly": bool, "servers": {"<server> or *": {"allow": [regex], "deny": [regex], "ask": [regex]}}}.
  No file = today's behaviour exactly. A file that is not valid JSON FAILS CLOSED: every call_tool returns
  `POLICY_DENIED policy file unreadable: ...`.
- Rules for a server are its own entry plus "*", merged. Regexes are re.search'ed on the BARE tool name. Order:
  readOnly (a tool whose annotations lack readOnlyHint: true is denied, rule "readOnly") > deny > ask > allow; when an
  `allow` list is given and nothing in it matches, the tool is denied with a rule naming "allow". Default allow all.
- Denied tools are invisible: list_capabilities, search_capabilities and list_groups_with_costs never show them.
- call_tool on a denied tool returns `POLICY_DENIED <rule>` without calling the backend.
- call_tool on an `ask` tool returns `CONFIRM_REQUIRED <token>` plus how to confirm; calling call_tool again with the
  SAME server, tool and args plus a top-level `confirm: <token>` runs it. `confirm` is NOT added to the call_tool
  schema (tools/list stays within its 1,100-token budget, descriptions unchanged). Tokens: HMAC-SHA256 under a
  per-process os.urandom(32) secret over server/tool/args-hash/nonce, TTL TOOL_GUARDIAN_CONFIRM_TTL (default 120 s)
  from issue, single use; a token for other args, another tool, a reused or an expired one never runs anything.
- tg_policy.Gate(ttl=120.0, clock=time.monotonic, secret=None) with issue(server, tool, args_hash) -> str and
  redeem(token, server, tool, args_hash) -> bool; tg_policy.args_hash(args) = sha256 of json.dumps(args,
  sort_keys=True) (hex).
- Every deny or ask writes a call-log row {"kind": "policy", "server", "target", "decision": "deny"|"ask", "rule"}.
  `--bypass-summary` prints a line `policy denials: <n>`.
- `tool-guardian [--config F] --policy-check <server> <tool> '<args-json>'` prints `allow`, `deny <rule>` or
  `ask <rule>` and exits 0 / 3 / 4. `--policy-check --list <server>` prints one line per tool of that server:
  `<allow|deny|ask> <tool>[ <rule>]`.
- README: a policy section with the three examples, "the model confirms, not you", and that annotations are the
  server's own untrusted hints.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tool_guardian as tg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    import tg_policy
except ImportError:  # RED on main
    tg_policy = None

ANNOTATED = r'''
import json, sys
TOOLS = [{"name": "read_file", "description": "Read a file.", "inputSchema": {"type": "object", "properties": {}},
          "annotations": {"readOnlyHint": True}},
         {"name": "write_file", "description": "Write a file.", "inputSchema": {"type": "object", "properties": {}},
          "annotations": {"readOnlyHint": False}},
         {"name": "touch", "description": "Touch.", "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m = msg["method"]
    if m == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fs", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    else:
        res = {"content": [{"type": "text", "text": "ran " + msg["params"]["name"]}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
'''


def notes():
    return {"command": sys.executable, "args": [str(ROOT / "tg_prove.py"), "--backend"]}


@pytest.fixture
def router(tmp_path, monkeypatch):
    made = []

    def make(policy, servers=None):
        pol = tmp_path / "policy.json"
        if policy is None:
            monkeypatch.setenv("TOOL_GUARDIAN_POLICY", str(tmp_path / "absent.json"))
        else:
            pol.write_text(policy if isinstance(policy, str) else json.dumps(policy), encoding="utf-8")
            monkeypatch.setenv("TOOL_GUARDIAN_POLICY", str(pol))
        fs = tmp_path / "fs_server.py"
        fs.write_text(ANNOTATED, encoding="utf-8")
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": servers or {"notes": notes(), "fs": {"command": sys.executable,
                                                                                      "args": [str(fs)]}}}),
                       encoding="utf-8")
        r = tg.Router(str(cfg))
        r.start()
        made.append(r)
        return r

    yield make
    for r in made:
        for b in r.backends.values():
            b.stop()


def call(r, server, tool, args=None, **extra):
    return r.handle("call_tool", dict({"server": server, "tool": tool, "args": args or {}}, **extra))


def policy_rows():
    p = Path(tg.CALL_LOG)
    return [x for x in (json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
            if x.get("kind") == "policy"] if p.exists() else []


def token_of(text):
    assert text.startswith("CONFIRM_REQUIRED "), text[:200]
    return text.split()[1]


# ---- no policy ---------------------------------------------------------------------------------------------------
def test_no_policy_file_is_unchanged(router):
    r = router(None)
    assert "counter: 4217" in call(r, "notes", "get_counter")
    cat = r.handle("list_capabilities", {"server": "notes"})
    assert all(t in cat for t in ("add_note", "find_notes", "get_counter"))
    assert policy_rows() == []


def test_confirm_is_not_in_the_schema_and_the_budget_holds():
    props = [t for t in tg.build_router_tools({}) if t["name"] == "call_tool"][0]["inputSchema"]["properties"]
    assert set(props) == {"server", "tool", "args"}


# ---- deny / allow / readOnly -------------------------------------------------------------------------------------
def test_deny_hides_the_tool_and_refuses_the_call(router):
    r = router({"servers": {"notes": {"deny": ["^add_"]}}})
    assert "add_note" not in r.handle("list_capabilities", {"server": "notes"})
    assert "add_note" not in r.handle("list_capabilities", {})
    assert "add_note" not in r.handle("search_capabilities", {"query": "add note"})
    if any(t["name"] == "list_groups_with_costs" for t in tg.build_router_tools(r.backends)):
        assert "add_note" not in r.handle("list_groups_with_costs", {})
    out = call(r, "notes", "add_note", {"title": "x"})
    assert out.startswith("POLICY_DENIED ") and "^add_" in out and "added note" not in out
    assert policy_rows()[-1] == {**policy_rows()[-1], "kind": "policy", "server": "notes", "target": "add_note",
                                 "decision": "deny"}


def test_deny_wins_over_ask_and_allow(router):
    r = router({"servers": {"*": {"allow": [".*"], "ask": ["get_"], "deny": ["get_counter"]}}})
    assert call(r, "notes", "get_counter").startswith("POLICY_DENIED ")


def test_an_allow_list_denies_the_rest(router):
    r = router({"servers": {"notes": {"allow": ["^get_"]}}})
    assert "counter: 4217" in call(r, "notes", "get_counter")
    out = call(r, "notes", "add_note", {"title": "x"})
    assert out.startswith("POLICY_DENIED ") and "allow" in out


def test_star_and_server_rules_merge(router):
    r = router({"servers": {"*": {"deny": ["^add_"]}, "notes": {"ask": ["^find_"]}}})
    assert call(r, "notes", "add_note", {"title": "x"}).startswith("POLICY_DENIED ")
    assert call(r, "notes", "find_notes", {"params": {"filters": []}}).startswith("CONFIRM_REQUIRED ")
    assert "counter: 4217" in call(r, "notes", "get_counter")


def test_read_only_uses_the_annotations(router):
    r = router({"readOnly": True})
    cat = r.handle("list_capabilities", {"server": "fs"})
    assert "read_file" in cat and "write_file" not in cat and "touch" not in cat
    assert "ran read_file" in call(r, "fs", "read_file")
    out = call(r, "fs", "write_file")
    assert out.startswith("POLICY_DENIED ") and "readOnly" in out


def test_read_only_on_an_unannotated_server_denies_everything(router):
    r = router({"readOnly": True})
    for tool in ("add_note", "find_notes", "get_counter"):
        assert call(r, "notes", tool).startswith("POLICY_DENIED readOnly"), tool
    assert "get_counter" not in r.handle("search_capabilities", {"query": "counter"})


def test_a_broken_policy_file_fails_closed(router):
    r = router("{not json")
    out = call(r, "notes", "get_counter")
    assert out.startswith("POLICY_DENIED policy file unreadable") and "4217" not in out


# ---- ask / tokens ------------------------------------------------------------------------------------------------
def test_ask_returns_a_token_then_runs(router):
    r = router({"servers": {"notes": {"ask": ["counter"]}}})
    first = call(r, "notes", "get_counter")
    tok = token_of(first)
    assert "confirm" in first and "4217" not in first
    assert "counter: 4217" in call(r, "notes", "get_counter", confirm=tok)
    assert [x["decision"] for x in policy_rows()] == ["ask"]


def test_a_reused_token_is_refused(router):
    r = router({"servers": {"notes": {"ask": ["counter"]}}})
    tok = token_of(call(r, "notes", "get_counter"))
    assert "4217" in call(r, "notes", "get_counter", confirm=tok)
    again = call(r, "notes", "get_counter", confirm=tok)
    assert again.startswith("CONFIRM_REQUIRED ") and "4217" not in again


def test_a_foreign_token_is_refused(router):
    r = router({"servers": {"notes": {"ask": [".*"]}}})
    tok = token_of(call(r, "notes", "find_notes", {"params": {"filters": []}}))
    other_args = call(r, "notes", "find_notes", {"params": {"filters": [{"field": "tag", "op": "eq", "value": "x"}]}},
                      confirm=tok)
    other_tool = call(r, "notes", "get_counter", confirm=tok)
    assert other_args.startswith("CONFIRM_REQUIRED ") and other_tool.startswith("CONFIRM_REQUIRED ")
    assert "4217" not in other_tool


def test_gate_expiry_single_use_and_secret():
    assert tg_policy is not None, "tg_policy.py is missing"
    now = [1000.0]
    g = tg_policy.Gate(ttl=120.0, clock=lambda: now[0])
    h = tg_policy.args_hash({"a": 1})
    assert h == tg_policy.args_hash({"a": 1}) and len(h) == 64 and h != tg_policy.args_hash({"a": 2})
    t1 = g.issue("s", "t", h)
    now[0] += 121
    assert g.redeem(t1, "s", "t", h) is False, "expired"
    t2 = g.issue("s", "t", h)
    assert g.redeem(t2, "s", "t", h) is True and g.redeem(t2, "s", "t", h) is False, "single use"
    a, b = tg_policy.Gate(secret=b"a" * 32), tg_policy.Gate(secret=b"b" * 32)
    ta = a.issue("s", "t", h)
    assert b.redeem(ta, "s", "t", h) is False, "another process secret cannot redeem it"
    assert a.redeem(ta, "s", "t", h) is True


def test_ttl_comes_from_the_environment(router, monkeypatch):
    monkeypatch.setenv("TOOL_GUARDIAN_CONFIRM_TTL", "0")
    r = router({"servers": {"notes": {"ask": ["counter"]}}})
    tok = token_of(call(r, "notes", "get_counter"))
    assert call(r, "notes", "get_counter", confirm=tok).startswith("CONFIRM_REQUIRED "), "a 0 s TTL never runs"


# ---- CLI and reports ---------------------------------------------------------------------------------------------
def cli(tmp_path, policy, *args):
    pol = tmp_path / "pc_policy.json"
    pol.write_text(json.dumps(policy), encoding="utf-8")
    cfg = tmp_path / "pc_mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"notes": notes()}}), encoding="utf-8")
    env = dict(os.environ, TOOL_GUARDIAN_POLICY=str(pol), GUARDIAN_NO_UPDATE_CHECK="1",
               TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "pc_calls.jsonl"), TOOL_GUARDIAN_STATE=str(tmp_path / "st.json"))
    return subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg), "--policy-check",
                           *args], capture_output=True, text=True, timeout=120, env=env)


def test_policy_check_exit_codes(tmp_path):
    pol = {"servers": {"notes": {"deny": ["^add_"], "ask": ["^find_"]}}}
    ok = cli(tmp_path, pol, "notes", "get_counter", "{}")
    no = cli(tmp_path, pol, "notes", "add_note", '{"title": "x"}')
    ask = cli(tmp_path, pol, "notes", "find_notes", "{}")
    assert (ok.returncode, ok.stdout.split()[0]) == (0, "allow"), ok.stdout + ok.stderr
    assert (no.returncode, no.stdout.split()[0]) == (3, "deny") and "^add_" in no.stdout
    assert (ask.returncode, ask.stdout.split()[0]) == (4, "ask") and "^find_" in ask.stdout


def test_policy_check_list_one_line_per_tool(tmp_path):
    r = cli(tmp_path, {"servers": {"notes": {"deny": ["^add_"], "ask": ["^find_"]}}}, "--list", "notes")
    lines = [line for line in r.stdout.splitlines() if line.strip()]
    assert r.returncode == 0 and len(lines) == 3, r.stdout + r.stderr
    got = {line.split()[1]: line.split()[0] for line in lines}
    assert got == {"add_note": "deny", "find_notes": "ask", "get_counter": "allow"}


def test_bypass_summary_counts_policy_denials(router):
    r = router({"servers": {"notes": {"deny": ["^add_"]}}})
    call(r, "notes", "add_note", {"title": "x"})
    assert "policy denials: 1" in tg.bypass_summary(session="all")


def test_readme_documents_the_policy():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "policy.json" in text and "TOOL_GUARDIAN_POLICY" in text and "--policy-check" in text
    assert "the model confirms, not you" in text.lower()
    assert "untrusted" in text.lower() and "readOnly" in text
