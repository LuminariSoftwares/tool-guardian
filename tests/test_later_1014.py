"""The "Later" list from the 2026-10-10 review triage, one section per item (2026-10-14).
Each test here failed on main before its change."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402


# ---- 1. nested validate_args -----------------------------------------------------

NESTED = {"type": "object", "required": ["params"], "properties": {
    "params": {"type": "object", "required": ["filters"], "properties": {
        "limit": {"type": "integer"},
        "filters": {"type": "array", "items": {
            "type": "object", "required": ["field", "op"], "additionalProperties": False,
            "properties": {"field": {"type": "string"},
                           "op": {"type": "string", "enum": ["eq", "lt", "gt"]},
                           "value": {"type": "number"}}}}}}}}


def _filters(*extra):
    return [{"field": "a", "op": "eq", "value": 1}, {"field": "b", "op": "lt", "value": 2}, *extra]


def test_nested_valid_call_passes_and_coerces_safely():
    ok, fixed, problems = tg.validate_args(NESTED, {"params": {"limit": "5", "filters": _filters(
        {"field": "c", "op": "gt", "value": "3.5"})}})
    assert (ok, problems) == (True, [])
    assert fixed["params"]["limit"] == 5 and fixed["params"]["filters"][2]["value"] == 3.5


def test_nested_enum_fault_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters(
        {"field": "c", "op": "like", "value": 1})}})
    assert not ok and problems == ["argument 'params.filters[2].op' must be one of: eq, lt, gt"]


def test_nested_missing_required_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters({"field": "c"})}})
    assert not ok and problems == ["missing required argument 'params.filters[2].op' (string)"]


def test_nested_type_fault_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters(
        {"field": "c", "op": "eq", "value": "lots"})}})
    assert not ok and problems == ["argument 'params.filters[2].value' should be number, got str"]


def test_nested_closed_object_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": [
        {"field": "a", "op": "eq", "colour": "red"}]}})
    assert not ok and problems == ["unknown argument 'params.filters[0].colour'; allowed: field, op, value"]


# ---- 2. respawn a dead backend ---------------------------------------------------

# Answers the handshake and ONE tools/call, then exits. Every spawn counts its own calls,
# and appends one line per spawn to $TG_SPAWN_LOG so the test can count initialize handshakes.
DIES_AFTER_FIRST_CALL = '''
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        with open(os.environ["TG_SPAWN_LOG"], "a") as fh:
            fh.write("initialize\\n")
    if mid is None:
        continue
    if method == "tools/list":
        res = {"tools": [{"name": "once", "description": "answers, then the server exits",
                          "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        res = {"content": [{"type": "text", "text": "answered pid %d" % os.getpid()}]}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
    sys.stdout.flush()
    if method == "tools/call":
        sys.exit(0)
'''


@pytest.fixture
def dying(tmp_path, monkeypatch):
    from test_review_1010 import _config, _kill_all
    log = tmp_path / "spawns.log"
    monkeypatch.setenv("TG_SPAWN_LOG", str(log))
    r = tg.Router(str(_config(tmp_path, {"flaky": DIES_AFTER_FIRST_CALL})))
    r.start()
    yield r, log
    _kill_all(r)


def _call_once(r):
    return r.handle("call_tool", {"server": "flaky", "tool": "once"})


def test_dead_backend_is_respawned_once_after_1s_then_marked_down(dying):
    import time
    r, log = dying
    b = r.backends["flaky"]
    first = _call_once(r)
    assert "answered pid" in first and r.stats["backend_respawns"] == 0
    b.proc.wait(timeout=5)                      # the backend has died
    t0 = time.monotonic()
    second = _call_once(r)                      # respawned, handshake re-sent, call answered
    took = time.monotonic() - t0
    assert "answered pid" in second and second.split()[2] != first.split()[2], (first, second)
    assert took >= 1.0, "respawn did not wait the 1 s backoff (%.2f s)" % took
    assert log.read_text().count("initialize") == 2
    assert r.stats["backend_respawns"] == 1 and b.respawns == 1
    b.proc.wait(timeout=5)                      # dead again, inside 60 s of the first failure
    with pytest.raises(RuntimeError, match="DOWN"):
        _call_once(r)
    assert b.status == "DOWN" and log.read_text().count("initialize") == 2   # no third spawn
    with pytest.raises(RuntimeError, match="DOWN"):
        _call_once(r)
    assert r.stats["backend_respawns"] == 1
    assert "[flaky] DOWN" in r.catalogue()


def test_second_respawn_outside_the_window_waits_5s(dying, monkeypatch):
    r, _log = dying
    b = r.backends["flaky"]
    monkeypatch.setattr(tg, "RESPAWN_WINDOW", 0.0)    # every failure is the first in its window
    waits = []
    monkeypatch.setattr(tg, "_backoff_sleep", lambda s: waits.append(round(s)))
    for _ in range(3):
        assert "answered pid" in _call_once(r)
        b.proc.wait(timeout=5)
    assert waits == [1, 5] and b.respawns == 2 and r.stats["backend_respawns"] == 2


def test_respawns_are_in_the_bridge_stats():
    assert tg.Router("").stats["backend_respawns"] == 0


# ---- 3. describe_tool detail=compact|full ----------------------------------------

def _fixture_router():
    from test_rank_identity import _router, _fixture_sets
    return _router(_fixture_sets()["bench"])


def test_describe_tool_compact_is_the_default():
    r = _fixture_router()
    out = r._handle("describe_tool", {"server": "github", "tool": "create_issue"})
    assert out == r._handle("describe_tool", {"server": "github", "tool": "create_issue", "detail": "compact"})
    lines = out.split("\n")
    assert lines[0] == "github.create_issue: Create a new issue in a GitHub repository"
    assert lines[1] == "required args: owner:string, repo:string, title:string"
    assert lines[2] == "" and lines[3].startswith("NEXT STEP: use call_tool with server github, tool create_issue, args: ")
    assert len(lines) == 4 and "inputSchema" not in out


def test_describe_tool_full_is_the_old_output():
    r = _fixture_router()
    t = r.backends["github"].find("create_issue")
    out = r._handle("describe_tool", {"server": "github", "tool": "create_issue", "detail": "full"})
    assert out == (json.dumps(t, indent=2) + "\n\nNEXT STEP: use call_tool with server github, tool create_issue, "
                   "args: %s." % tg.compact_args(t["inputSchema"]))


def test_describe_tool_advertises_detail():
    schema = {t["name"]: t for t in tg.build_router_tools({})}["describe_tool"]["inputSchema"]
    assert schema["properties"]["detail"] == {"enum": ["compact", "full"]}
    assert schema["required"] == ["server", "tool"]


def test_describe_tool_compact_is_at_least_40_percent_shorter_on_the_bench_fixtures():
    r = _fixture_router()
    compact = full = 0
    for name, b in sorted(r.backends.items()):
        for t in b.tools:
            args = {"server": name, "tool": t["name"]}
            compact += len(r._handle("describe_tool", dict(args, detail="compact")))
            full += len(r._handle("describe_tool", dict(args, detail="full")))
    saved = 1 - compact / full
    print("describe_tool over 95 bench tools: compact %d chars, full %d chars, %.1f%% shorter"
          % (compact, full, 100 * saved))
    assert saved >= 0.40


# ---- 4. --prove ------------------------------------------------------------------

def _stub_endpoint(stall):
    """An OpenAI-compatible /v1/chat/completions that plays a model by script. Every reply
    reports usage prompt 100 / completion 10. stall=True: on the counter prompt it only
    searches and then gives up -- the stalled session."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def call(name, args):
        return {"tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}

    def reply(messages):
        user = next(m["content"] for m in messages if m["role"] == "user")
        tools = [m["content"] for m in messages if m["role"] == "tool"]
        if "add_note" in user:
            steps = [call("list_capabilities", {"server": "notes"}),
                     call("describe_tool", {"server": "notes", "tool": "add_note"}),
                     {"content": "add_note requires title."}]
        elif "find_notes" in user:
            steps = [call("call_tool", {"server": "notes", "tool": "find_notes", "args": {
                "params": {"filters": [{"field": "tag", "op": "eq", "value": "urgent"}]}}}),
                     {"content": "ids: " + (tools[-1].split("\n")[0] if tools else "")}]
        elif stall:
            steps = [call("search_capabilities", {"query": "counter"})] * 3 + [{"content": "I could not find it."}]
        else:
            steps = [call("search_capabilities", {"query": "counter"}),
                     call("call_tool", {"server": "notes", "tool": "get_counter"}),
                     {"content": tools[-1].split("\n")[0] if tools else ""}]
        return steps[min(len(tools), len(steps) - 1)]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self._send({"data": [{"id": "stub-model"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            msg = dict({"role": "assistant", "content": None}, **reply(body["messages"]))
            self._send({"choices": [{"message": msg}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

        def _send(self, obj):
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _prove(env_extra, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOOL_GUARDIAN_PROVE_")}
    env.update(env_extra, TOOL_GUARDIAN_CALL_LOG=str(tmp_path / "calls.jsonl"))
    return subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--prove"], cwd=str(tmp_path),
                          capture_output=True, text=True, env=env, timeout=120)


def test_prove_without_an_endpoint_names_the_env_var_and_exits_2(tmp_path):
    r = _prove({}, tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "set TOOL_GUARDIAN_PROVE_URL to an OpenAI-compatible base URL" in r.stdout


@pytest.mark.parametrize("stall", [False, True])
def test_prove_against_a_stub_endpoint(tmp_path, stall):
    srv = _stub_endpoint(stall)
    try:
        r = _prove({"TOOL_GUARDIAN_PROVE_URL": "http://127.0.0.1:%d/v1" % srv.server_address[1]}, tmp_path)
    finally:
        srv.shutdown()
    lines = {ln.split()[1]: ln for ln in r.stdout.splitlines() if ln.startswith(("PASS ", "FAIL "))}
    assert sorted(lines) == ["list_describe", "nested_call", "stalled"], r.stdout + r.stderr
    assert "model stub-model" in r.stdout
    assert lines["list_describe"].startswith("PASS") and "prompt 300, completion 30, total 330" in lines["list_describe"]
    assert lines["nested_call"].startswith("PASS") and "prompt 200, completion 20, total 220" in lines["nested_call"]
    if stall:
        assert lines["stalled"].startswith("FAIL") and "stalled: 3 discovery call(s), no call_tool" in lines["stalled"]
        assert r.returncode == 1
    else:
        assert lines["stalled"].startswith("PASS") and "prompt 300, completion 30" in lines["stalled"]
        assert r.returncode == 0, r.stdout
    assert not (tmp_path / "calls.jsonl").exists()      # a proof run is not logged as a session


# ---- 5. hook --rules <file> -------------------------------------------------------

def _hook_run(tmp_path, payload, *args):
    from test_claude_code_hook import _hook
    return _hook(tmp_path, payload, {"jobs": ["render_video"]}, *args)


def _bash(command):
    from test_claude_code_hook import DOCS
    payload = json.loads(json.dumps(DOCS))
    payload["tool_input"]["command"] = command
    return payload


def _rules(tmp_path, rules):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return str(path)


def test_hook_rules_allow_lets_a_router_tool_command_through(tmp_path):
    from test_claude_code_hook import _rows
    rules = _rules(tmp_path, {"allow": [{"tool": "Bash", "args": {"command": r"^python scripts/render_video --dry"}}]})
    r = _hook_run(tmp_path, _bash("python scripts/render_video --dry-run"), "--mode", "deny", "--rules", rules)
    assert r.returncode == 0 and r.stdout == "" and _rows(tmp_path) == []
    # a command the allow pattern does not cover still gets the default behaviour
    r = _hook_run(tmp_path, _bash("python scripts/render_video --all"), "--mode", "deny", "--rules", rules)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_rules_deny_by_argument_regex_and_by_tool_name(tmp_path):
    from test_claude_code_hook import _assert_documented_deny
    rules = _rules(tmp_path, {"deny": [
        {"tool": "Bash", "args": {"command": r"\brm\s+-rf\b"}, "reason": "no recursive deletes"},
        {"tool": "WebFetch"}]})
    r = _hook_run(tmp_path, _bash("rm -rf build"), "--rules", rules)        # default mode is log
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert r.returncode == 0 and out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "no recursive deletes"
    fetch = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "WebFetch",
             "tool_input": {"url": "https://example.com"}}
    r = _hook_run(tmp_path, fetch, "--rules", rules)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"].startswith(
        "denied by the tool-guardian rules file")
    # deny wins over allow; a command no rule matches gets the default behaviour (here: deny)
    both = _rules(tmp_path, {"allow": [{"tool": "Bash"}], "deny": [{"tool": "Bash", "args": {"command": "render"}}]})
    r = _hook_run(tmp_path, _bash("python scripts/render_video --all"), "--rules", both)
    assert "rules file" in json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    r = _hook_run(tmp_path, _bash("python scripts/render_video --all"), "--mode", "deny",
                  "--rules", _rules(tmp_path, {"deny": [{"tool": "Bash", "args": {"command": "^ls"}}]}))
    _assert_documented_deny(r.stdout, "jobs", "render_video")


@pytest.mark.parametrize("rules_arg", [None, "missing.json", "broken"])
def test_hook_rules_absent_or_unreadable_keeps_the_default_behaviour(tmp_path, rules_arg):
    from test_claude_code_hook import _assert_documented_deny
    args = ["--mode", "deny"]
    if rules_arg == "broken":
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        args += ["--rules", str(tmp_path / "broken.json")]
    elif rules_arg:
        args += ["--rules", str(tmp_path / rules_arg)]
    r = _hook_run(tmp_path, _bash("python scripts/render_video --all"), *args)
    assert r.returncode == 0
    _assert_documented_deny(r.stdout, "jobs", "render_video")
    r = _hook_run(tmp_path, _bash("ls -la"), *args)
    assert r.returncode == 0 and r.stdout == ""


# ---- 6. selftest counts with tiktoken when importable -----------------------------

class _FakeEncoding:
    def encode(self, text):
        return list(range(len(text) // 2))      # 2 chars per token: easy to tell from the estimate


def _selftest_out(tmp_path, capsys):
    from test_review_1010 import _config
    from test_tool_guardian import STUB_SERVER
    code = tg.selftest(str(_config(tmp_path, {"stub": STUB_SERVER})))
    return code, capsys.readouterr().out


def test_selftest_uses_tiktoken_when_importable(tmp_path, capsys, monkeypatch):
    import types
    fake = types.ModuleType("tiktoken")
    fake.get_encoding = lambda name: _FakeEncoding() if name == "cl100k_base" else None
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    code, out = _selftest_out(tmp_path, capsys)
    assert code == 0 and "token counts: tiktoken cl100k_base" in out, out
    expected = len(json.dumps(tg.ROUTER_TOOLS, ensure_ascii=False)) // 2
    assert "router tools cost ~%d tokens" % expected in out, out


def test_selftest_falls_back_to_the_estimate_without_tiktoken(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "tiktoken", None)          # import tiktoken -> ImportError
    code, out = _selftest_out(tmp_path, capsys)
    assert code == 0 and "token counts: estimate (chars / 3.5; tiktoken not available)" in out, out
    assert "router tools cost ~%d tokens" % tg.est_tokens(tg.ROUTER_TOOLS) in out, out


# ---- 7. TOOL_GUARDIAN_PRESET=small --------------------------------------------------

_TUNABLES = ("START_TIMEOUT", "CALL_TIMEOUT", "CHARS_PER_TOKEN", "LOG_PATH", "SKILLS_DIRS", "LADDER_ON",
             "RAW_RESULTS", "CALL_LOG", "DESCRIBE_DETAIL", "CATALOGUE_CHARS", "DESC_WORDS")


def _tunables(monkeypatch, preset):
    if preset is None:
        monkeypatch.delenv("TOOL_GUARDIAN_PRESET", raising=False)
    else:
        monkeypatch.setenv("TOOL_GUARDIAN_PRESET", preset)
    for name in ("TOOL_GUARDIAN_CATALOGUE_CHARS", "TOOL_GUARDIAN_DESC_WORDS"):
        monkeypatch.delenv(name, raising=False)
    tg.read_tunables()
    import tg_ladder
    return {n: getattr(tg, n, "<missing>") for n in _TUNABLES}, dict(tg_ladder.DEFAULTS)


@pytest.fixture
def restore_tunables(monkeypatch):
    yield
    monkeypatch.undo()
    tg.read_tunables()


def test_small_preset_changes_only_the_documented_values(monkeypatch, restore_tunables):
    default, ladder_default = _tunables(monkeypatch, None)
    small, ladder_small = _tunables(monkeypatch, "small")
    changed = {n: (default[n], small[n]) for n in _TUNABLES if default[n] != small[n]}
    assert changed == {"CATALOGUE_CHARS": (0, 1500), "DESC_WORDS": (12, 6)}
    assert ladder_small == ladder_default                          # ladder rules untouched
    assert default["DESCRIBE_DETAIL"] == small["DESCRIBE_DETAIL"] == "compact"
    assert tg.PRESETS["small"] == {"catalogue_chars": 1500, "desc_words": 6}
    monkeypatch.setenv("TOOL_GUARDIAN_DESC_WORDS", "9")            # an explicit value wins over the preset
    tg.read_tunables()
    assert tg.DESC_WORDS == 9 and tg.CATALOGUE_CHARS == 1500


def test_small_preset_caps_each_server_listing_and_shortens_descriptions(monkeypatch, restore_tunables):
    _tunables(monkeypatch, "small")
    r = _fixture_router()
    listing = r.catalogue("github").split("\n\nNEXT STEP")[0]
    body, _, note = listing.rpartition("\n")
    assert len(body) <= 1500, len(body)
    assert note.startswith("[+") and "more tools not shown (TOOL_GUARDIAN_CATALOGUE_CHARS=1500)" in note
    assert "search_capabilities with a keyword and server github" in note
    first = body.split("\n")[0]
    assert first == "github.create_or_update_file: Create or update a single file..."
    assert r.backends["memory"].hint().count(" ") <= 6
    # ranking still scores on the full 12-word description
    assert [ln.split(":")[0] for ln in r.search("read file").splitlines()[1:4:2]] == \
        [ln.split(":")[0] for ln in json.loads((ROOT / "tests" / "fixtures" / "rank_golden.json").read_text(
            encoding="utf-8"))["bench"]["search"]["read file"].splitlines()[1:4:2]]


def test_no_preset_leaves_the_listing_whole(monkeypatch, restore_tunables):
    _tunables(monkeypatch, None)
    r = _fixture_router()
    assert "more tools not shown" not in r.catalogue("github")
    assert r.catalogue("github").count("\n    args: ") == 26
