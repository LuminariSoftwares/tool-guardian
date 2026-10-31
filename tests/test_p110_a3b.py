"""P110 S2b (A3b) acceptance -- the human gate: an `ask` rule asks the HUMAN when the client can show a dialog.
Written by the O5 overseer; DO NOT EDIT (and do not edit tests/fixtures/p110_a3b_floor.jsonl: its sha256 is below).

Why: S2's confirm token is a gate the MODEL passes, not the user. MCP clients that offer elicitation (Claude Code in an
interactive terminal does) can put a yes/no in front of the human. A3b uses it when -- and only when -- the client
negotiated a protocol that has elicitation AND advertised the capability; every other client keeps S2's token, and a
2024-11-05 client sees the router byte-for-byte as it was (plan P110 decision 6, C4 "A3b", kill criterion K1b).

Contract (plan P110 decision 6 + C4 A3b; spec 2025-11-25 client/elicitation and 2026-07-28 basic/patterns/mrtr; pinned
by O5 2026-10-31):
- Client-facing version (legacy `initialize`): the router answers with the client's requested `protocolVersion` when it
  is one of 2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05, else with 2025-11-25 (the newest legacy revision; spec:
  "SHOULD be the latest version supported"). The reply's `capabilities` stay {"tools": {}}. Backends keep 2024-11-05.
  `server/discover` stays -32601 (test_review_1021 pins it), so a dual-era client probing it falls back to initialize.
- Elicitation is used only when the negotiated version is 2025-06-18 or 2025-11-25 AND the client's `initialize`
  capabilities carry `elicitation` with form mode (`{}` or a dict with a "form" key; spec: `{}` == form only).
  Otherwise (2024-11-05, 2025-03-26, no capability, url-only) an `ask` rule is S2's CONFIRM_REQUIRED token, unchanged.
- Legacy elicitation: while handling the call_tool, the router writes ONE JSON-RPC request to stdout:
  {"jsonrpc": "2.0", "id": <any non-null id>, "method": "elicitation/create", "params": {"message": ...,
  "requestedSchema": {"type": "object", "properties": {"confirm": {"type": "boolean", ...}}, "required": ["confirm"]}}}
  (`mode` absent or "form"). `message` names `<server>.<tool>` and contains the call's args as compact JSON when that
  JSON is <= 200 characters. It then reads stdin until the response with that id arrives; requests the client sends
  meanwhile are still answered. The tool runs ONLY on result {"action": "accept", "content": {"confirm": true}}.
  Anything else -- decline, cancel, accept without confirm true, a JSON-RPC error, no answer within
  TOOL_GUARDIAN_ELICIT_TIMEOUT seconds (default 300), or stdin closing -- returns call_tool text starting
  `USER_DECLINED <outcome>` with outcome one of decline, cancel, unconfirmed, error, timeout, and nothing runs. A reply
  that arrives after its timeout is ignored. stdin closing mid-wait ends the router cleanly (exit 0).
- On an elicitation-capable client no confirm token is ever issued: no reply text contains CONFIRM_REQUIRED.
- Each elicitation writes a call-log row {"kind": "policy", "decision": "ask", "via": "elicitation",
  "outcome": "accept"|<the declined outcome>, ...}.
- Modern 2026-07-28 (stateless; no initialize): a `tools/call` whose params._meta carries
  "io.modelcontextprotocol/protocolVersion": "2026-07-28" and "io.modelcontextprotocol/clientCapabilities" with
  form elicitation gets, for an `ask` tool, the result {"resultType": "input_required", "inputRequests": {<key>:
  {"method": "elicitation/create", "params": <as above>}}, "requestState": <opaque string>} -- exactly one request.
  The retry (a NEW id, same name + arguments, plus params.inputResponses {<key>: <ElicitResult>} and the echoed
  params.requestState) runs the tool on accept + confirm true and returns resultType "complete". requestState is
  integrity-protected and single use, bound to server, tool and args: reused, tampered or other-args state never runs
  the tool (the router answers input_required again). A decline returns resultType "complete" with USER_DECLINED text.
  Capabilities are per request: a modern call without elicitation gets the S2 token. Every modern tools/call result
  carries "resultType".
"""
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FLOOR = ROOT / "tests" / "fixtures" / "p110_a3b_floor.jsonl"
FLOOR_SHA256 = "eb511bac8054223a4e31c5cf1ed91fa90afedf930e48346159ec0232ab995ee4"
MODERN = "2026-07-28"

# A stdio backend. Each call appends the tool name to ran.txt in the router's working directory, so a test can tell
# whether the tool actually ran (the reply text alone could lie).
FS = r'''
import json, sys
TOOLS = [{"name": "read_file", "description": "Read a file.", "inputSchema": {"type": "object", "properties": {}},
          "annotations": {"readOnlyHint": True}},
         {"name": "write_file", "description": "Write a file.", "inputSchema": {"type": "object", "properties": {}},
          "annotations": {"readOnlyHint": False}}]
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
        with open("ran.txt", "a") as fh:
            fh.write(msg["params"]["name"] + "\n")
        res = {"content": [{"type": "text", "text": "ran " + msg["params"]["name"]}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
'''
POLICY = {"servers": {"fs": {"ask": ["^write_"]}}}
ARGS = {"path": "notes/a.txt", "text": "hello"}


class Session:
    """tool-guardian as a stdio MCP server, spoken to line by line (the client side of a real session)."""

    def __init__(self, tmp_path, env_extra=None):
        (tmp_path / "fs_server.py").write_text(FS, encoding="utf-8")
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"fs": {"command": sys.executable, "args": [str(tmp_path / "fs_server.py")]}}}),
                       encoding="utf-8")
        (tmp_path / "policy.json").write_text(json.dumps(POLICY), encoding="utf-8")
        self.tmp, self.log = tmp_path, tmp_path / "calls.jsonl"
        env = dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1", TOOL_GUARDIAN_POLICY=str(tmp_path / "policy.json"),
                   TOOL_GUARDIAN_CALL_LOG=str(self.log), PYTHONIOENCODING="utf-8")
        for k in ("TOOL_GUARDIAN_SKILLS", "TOOL_GUARDIAN_LADDER", "TOOL_GUARDIAN_PRESET", "TOOL_GUARDIAN_TERSE",
                  "TOOL_GUARDIAN_ELICIT_TIMEOUT", "TOOL_GUARDIAN_CONFIRM_TTL"):
            env.pop(k, None)
        env.update(env_extra or {})
        self.p = subprocess.Popen([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  cwd=str(tmp_path), env=env, text=True, encoding="utf-8", bufsize=1)
        self.raw, self.q = [], queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            if line.strip():
                self.raw.append(line.rstrip("\r\n"))
                self.q.put(json.loads(line))
        self.q.put(None)

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def next(self, timeout=60.0):
        msg = self.q.get(timeout=timeout)
        assert msg is not None, "the router closed stdout"
        return msg

    def ran(self):
        f = self.tmp / "ran.txt"
        return f.read_text(encoding="utf-8").split() if f.exists() else []

    def rows(self):
        if not self.log.exists():
            return []
        return [r for r in (json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip())
                if r.get("kind") == "policy"]

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


@pytest.fixture
def session(tmp_path):
    made = []

    def make(**env):
        s = Session(tmp_path, env)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def initialize(s, version, caps=None, mid=1):
    s.send({"jsonrpc": "2.0", "id": mid, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": caps or {}, "clientInfo": {"name": "t", "version": "1"}}})
    got = s.next()
    s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert got.get("id") == mid, got
    return got["result"]


def call_tool(mid, tool="write_file", args=None, confirm=None, **params):
    """A tools/call of the router's call_tool. `confirm` is S2's top-level call_tool argument; `params` go on the
    JSON-RPC params (modern `_meta`, `inputResponses`, `requestState`)."""
    a = {"server": "fs", "tool": tool, "args": ARGS if args is None else args}
    if confirm is not None:
        a["confirm"] = confirm
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": dict({"name": "call_tool", "arguments": a}, **params)}


def text_of(msg):
    return "".join(c.get("text", "") for c in msg["result"].get("content") or [])


def elicitation(s):
    req = s.next()
    assert req.get("method") == "elicitation/create" and req.get("id") is not None, req
    return req


def accept(confirm=True):
    return {"action": "accept", "content": {"confirm": confirm}}


def modern_meta(caps):
    return {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": caps,
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"}}


# ---- the 2024-11-05 floor: byte-identical to main before A3b -----------------------------------------------------
def normalise(line):
    line = re.sub(r'"serverInfo": \{"name": "tool-guardian", "version": "[^"]*"\}',
                  '"serverInfo": {"name": "tool-guardian", "version": "<v>"}', line)
    return re.sub(r"\b[0-9a-f]{32}\b", "<token>", line)      # S2 confirm tokens: 32 hex chars


def floor_session(s):
    """The session main answered at 6ecadbf: initialize 2024-11-05, tools/list, an ask tool (token), its confirm."""
    initialize(s, "2024-11-05", {"elicitation": {}})   # elicitation did not exist in 2024-11-05: must be ignored
    s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    s.next()
    s.send(call_tool(3))
    token = text_of(s.next()).split()[1]
    s.send(call_tool(4, confirm=token))
    s.next()
    s.send(call_tool(5, tool="read_file", args={}))
    s.next()
    return [normalise(x) for x in s.raw]


def test_floor_fixture_is_the_one_o5_recorded():
    assert hashlib.sha256(FLOOR.read_bytes()).hexdigest() == FLOOR_SHA256


def test_a_2024_11_05_client_sees_the_router_byte_for_byte_as_before(session):
    s = session()
    got = floor_session(s)
    want = FLOOR.read_text(encoding="utf-8").splitlines()
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want)):
        assert g == w, "line %d differs\n got: %s\nwant: %s" % (i, g[:400], w[:400])
    assert s.ran() == ["write_file", "read_file"]


# ---- version negotiation -------------------------------------------------------------------------------------
@pytest.mark.parametrize("asked,answered", [("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"),
                                            ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                                            ("2099-01-01", "2025-11-25")])
def test_initialize_answers_the_negotiated_version(session, asked, answered):
    s = session()
    res = initialize(s, asked)
    assert res["protocolVersion"] == answered and res["capabilities"] == {"tools": {}}


@pytest.mark.parametrize("version,caps", [("2025-06-18", {}), ("2025-11-25", {"elicitation": {"url": {}}}),
                                          ("2025-03-26", {"elicitation": {}}), ("2024-11-05", {"elicitation": {}})])
def test_no_capable_elicitation_means_the_s2_token(session, version, caps):
    s = session()
    initialize(s, version, caps)
    s.send(call_tool(2))
    msg = s.next()
    assert msg.get("id") == 2 and "method" not in msg, "no elicitation/create may be sent: %r" % msg
    assert text_of(msg).startswith("CONFIRM_REQUIRED ") and s.ran() == []


# ---- legacy elicitation (2025-06-18 / 2025-11-25) ------------------------------------------------------------
@pytest.mark.parametrize("version,caps", [("2025-11-25", {"elicitation": {"form": {}}}), ("2025-11-25", {"elicitation": {}}),
                                          ("2025-06-18", {"elicitation": {}})])
def test_accept_with_confirm_true_runs_the_tool(session, version, caps):
    s = session()
    initialize(s, version, caps)
    s.send(call_tool(2))
    req = elicitation(s)
    p = req["params"]
    assert p.get("mode", "form") == "form"
    schema = p["requestedSchema"]
    assert schema["type"] == "object" and schema["properties"]["confirm"]["type"] == "boolean"
    assert "confirm" in schema["required"]
    assert "fs.write_file" in p["message"] and json.dumps(ARGS, separators=(",", ":")) in p["message"]
    assert s.ran() == [], "nothing may run before the human answers"
    s.send({"jsonrpc": "2.0", "id": req["id"], "result": accept()})
    done = s.next()
    assert done.get("id") == 2 and text_of(done).startswith("ran write_file"), done
    assert s.ran() == ["write_file"]
    assert [r for r in s.rows() if r.get("via") == "elicitation" and r.get("outcome") == "accept"]


@pytest.mark.parametrize("answer,outcome", [({"result": {"action": "decline"}}, "decline"),
                                            ({"result": {"action": "cancel"}}, "cancel"),
                                            ({"result": accept(False)}, "unconfirmed"),
                                            ({"result": {"action": "accept", "content": {}}}, "unconfirmed"),
                                            ({"error": {"code": -32602, "message": "no"}}, "error")])
def test_anything_but_an_explicit_yes_runs_nothing(session, answer, outcome):
    s = session()
    initialize(s, "2025-11-25", {"elicitation": {}})
    s.send(call_tool(2))
    req = elicitation(s)
    s.send(dict({"jsonrpc": "2.0", "id": req["id"]}, **answer))
    done = s.next()
    t = text_of(done)
    assert done.get("id") == 2 and t.startswith("USER_DECLINED " + outcome), t[:200]
    assert "CONFIRM_REQUIRED" not in t and s.ran() == []
    assert [r for r in s.rows() if r.get("via") == "elicitation" and r.get("outcome") == outcome]


def test_a_capable_client_is_never_given_a_token_even_if_the_model_sends_one(session):
    s = session()
    initialize(s, "2025-11-25", {"elicitation": {}})
    s.send(call_tool(2, confirm="0" * 64))
    req = elicitation(s)
    s.send({"jsonrpc": "2.0", "id": req["id"], "result": {"action": "decline"}})
    assert "CONFIRM_REQUIRED" not in text_of(s.next()) and s.ran() == []
    s.send(call_tool(3, tool="read_file", args={}))
    assert text_of(s.next()).startswith("ran read_file"), "allowed tools are not asked about"


def test_requests_sent_while_waiting_are_still_answered(session):
    s = session()
    initialize(s, "2025-11-25", {"elicitation": {}})
    s.send(call_tool(2))
    req = elicitation(s)
    s.send({"jsonrpc": "2.0", "id": 77, "method": "ping"})
    s.send({"jsonrpc": "2.0", "id": req["id"], "result": accept()})
    got = {m.get("id"): m for m in (s.next(), s.next())}
    assert got[77]["result"] == {} and text_of(got[2]).startswith("ran write_file")


def test_no_answer_times_out_and_a_late_answer_is_ignored(session):
    s = session(TOOL_GUARDIAN_ELICIT_TIMEOUT="1")
    initialize(s, "2025-11-25", {"elicitation": {}})
    t0 = time.monotonic()
    s.send(call_tool(2))
    req = elicitation(s)
    done = s.next(timeout=30)
    assert text_of(done).startswith("USER_DECLINED timeout") and time.monotonic() - t0 < 20
    s.send({"jsonrpc": "2.0", "id": req["id"], "result": accept()})
    s.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert s.next().get("id") == 3 and s.ran() == []


def test_stdin_closing_mid_wait_ends_the_router_cleanly(session):
    s = session()
    initialize(s, "2025-11-25", {"elicitation": {}})
    s.send(call_tool(2))
    elicitation(s)
    s.p.stdin.close()
    assert s.p.wait(timeout=30) == 0 and s.ran() == []


# ---- modern 2026-07-28: MRTR input_required ------------------------------------------------------------------
def modern_call(mid, caps, **extra):
    msg = call_tool(mid, _meta=modern_meta(caps))
    msg["params"].update(extra)
    return msg


def asked(s, mid=10, caps=None):
    s.send(modern_call(mid, {"elicitation": {}} if caps is None else caps))
    res = s.next()["result"]
    assert res.get("resultType") == "input_required", res
    reqs = res["inputRequests"]
    assert len(reqs) == 1
    key, req = next(iter(reqs.items()))
    assert req["method"] == "elicitation/create"
    assert req["params"]["requestedSchema"]["properties"]["confirm"]["type"] == "boolean"
    assert isinstance(res.get("requestState"), str) and res["requestState"]
    return key, res["requestState"]


def test_modern_input_required_then_the_retry_runs(session):
    s = session()
    key, state = asked(s)
    assert s.ran() == []
    s.send(modern_call(11, {"elicitation": {}}, inputResponses={key: accept()}, requestState=state))
    res = s.next()["result"]
    assert res.get("resultType") == "complete" and text_of({"result": res}).startswith("ran write_file")
    assert s.ran() == ["write_file"]


def test_modern_state_is_single_use_bound_and_tamper_proof(session):
    s = session()
    key, state = asked(s)
    s.send(modern_call(11, {"elicitation": {}}, inputResponses={key: accept()}, requestState=state))
    assert s.next()["result"]["resultType"] == "complete"
    s.send(modern_call(12, {"elicitation": {}}, inputResponses={key: accept()}, requestState=state))
    assert s.next()["result"]["resultType"] == "input_required", "reused state must not run the tool again"
    key2, state2 = asked(s, mid=13)
    other = modern_call(14, {"elicitation": {}}, inputResponses={key2: accept()}, requestState=state2)
    other["params"]["arguments"]["args"] = {"path": "other.txt"}
    s.send(other)
    assert s.next()["result"]["resultType"] == "input_required", "state for other args must not run"
    key3, state3 = asked(s, mid=15)
    bad = state3[:-1] + ("A" if state3[-1] != "A" else "B")
    s.send(modern_call(16, {"elicitation": {}}, inputResponses={key3: accept()}, requestState=bad))
    assert s.next()["result"]["resultType"] == "input_required", "tampered state must not run"
    assert s.ran() == ["write_file"]


def test_modern_decline_and_no_capability(session):
    s = session()
    key, state = asked(s)
    s.send(modern_call(11, {"elicitation": {}}, inputResponses={key: {"action": "decline"}}, requestState=state))
    res = s.next()["result"]
    assert res["resultType"] == "complete" and text_of({"result": res}).startswith("USER_DECLINED decline")
    s.send(modern_call(12, {}))
    res = s.next()["result"]
    assert res.get("resultType") == "complete" and text_of({"result": res}).startswith("CONFIRM_REQUIRED ")
    assert s.ran() == []
