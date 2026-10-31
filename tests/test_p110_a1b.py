"""P110 S1b acceptance (A1c): the deprecated 2024-11-05 HTTP+SSE transport. Written by the O5 overseer; DO NOT EDIT.

Why: S1 speaks Streamable HTTP, modern and legacy. Some servers in the wild still speak only the older HTTP+SSE
transport (deprecated upstream since 2025-03-26, reclassified Deprecated in 2026-07-28, still eligible to exist
for a twelve-month window). Today a `"type": "sse"` entry is reported `UNSUPPORTED (SSE lands in 0.9.1)`. S1b makes
it work, so 0.9.0 ships with no transport asterisk (plan P110 decision 1 (c), C1 era "sse", C3 `--era sse`).

Contract (plan P110 decision 1 (c) + C1 + spec 2024-11-05 basic/transports "HTTP with SSE"; pinned by O5 2026-10-31):
- HttpTransport(url, headers, kind="sse") speaks HTTP+SSE directly: the FIRST request is a GET of `url` with
  `Accept: text/event-stream`; the first event is `endpoint`, whose data is the URI to POST to (a path is resolved
  against `url`'s origin; an absolute URL is used as given). Then `initialize` (protocolVersion 2024-11-05) and
  `notifications/initialized` are POSTed there, and every reply is read from the GET stream by its id.
  `.era == "sse"`, `.protocol_version == "2024-11-05"`, `.server_info` = the initialize result's serverInfo.
- kind="auto" (and "http") falls back to it: a modern `server/discover` POST answered 400/404/405 without a modern
  error, then a legacy `initialize` POST answered 400/404/405, then a GET whose first event is `endpoint` -> era sse.
  In that order, exactly once each.
- The user's headers (a `${VAR}` bearer included) go on EVERY request: the GET and every POST.
- A 401 is TransportError kind "auth".
- The stream closing (or a POST to the endpoint answered 404) reconnects ONCE: a new GET, a new endpoint, a new
  initialize; the request that hit it is re-issued with a new id and succeeds. A second failure raises.
- A reply that does not arrive within the call timeout is TransportError kind "timeout".
- close() ends the GET stream and sends no DELETE.
- Backend: `{"url": ..., "type": "sse"}` is live (status ok, tools listed, calls work); `--selftest` prints
  `[remote] <name>: sse 2024-11-05 <n> tools`. Nothing reports "SSE lands in 0.9.1" any more.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tool_guardian as tg

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "tests" / "http_stub_server.py"
sys.path.insert(0, str(ROOT))
import tg_http  # noqa: E402  (S1 shipped it; S1b extends it)


class Stub:
    def __init__(self, tmp_path, *args):
        self.log = tmp_path / ("stub_%d.jsonl" % time.monotonic_ns())
        self.proc = subprocess.Popen([sys.executable, str(STUB), "--log", str(self.log), *args],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().strip()
        assert line.startswith("STUB READY"), line + self.proc.stderr.read()
        kv = dict(part.split("=") for part in line.split()[2:])
        self.port = int(kv["port"])
        self.url = "http://127.0.0.1:%d/mcp" % self.port

    def rows(self):
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def gets(self):
        return [r for r in self.rows() if r.get("http") == "GET"]

    def posts(self, method=None):
        return [r for r in self.rows() if r.get("http") == "POST" and isinstance(r.get("body"), dict)
                and (method is None or r["body"].get("method") == method)]

    def stop(self):
        self.proc.kill()
        self.proc.communicate(timeout=10)


@pytest.fixture
def stub(tmp_path):
    made = []

    def start(*args):
        s = Stub(tmp_path, "--era", "sse", *args)
        made.append(s)
        return s

    yield start
    for s in made:
        s.stop()


def transport(url, kind="sse", **kw):
    kw.setdefault("start_timeout", 10.0)
    kw.setdefault("call_timeout", 10.0)
    return tg_http.HttpTransport(url, kw.pop("headers", {}), kind=kind, **kw)


def wait_for(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ---- the transport --------------------------------------------------------------------------------------
def test_forced_sse_opens_the_stream_first_and_initializes_on_the_endpoint(stub):
    s = stub()
    t = transport(s.url)
    t.start()
    try:
        assert t.era == "sse" and t.protocol_version == "2024-11-05"
        assert (t.server_info or {}).get("name") == "stub-sse"
        rows = s.rows()
        assert rows[0]["http"] == "GET" and rows[0]["path"] == "/mcp", rows[:3]
        assert "text/event-stream" in (rows[0]["headers"].get("accept") or "")
        assert s.posts("server/discover") == [], "a forced sse entry never probes the newer transports"
        init = s.posts("initialize")
        assert len(init) == 1 and init[0]["path"].startswith("/messages?session=sse-1")
        assert init[0]["body"]["params"]["protocolVersion"] == "2024-11-05"
        assert wait_for(lambda: len(s.posts("notifications/initialized")) == 1)
    finally:
        t.close()


def test_replies_come_back_on_the_stream(stub):
    s = stub()
    t = transport(s.url)
    t.start()
    try:
        assert [x["name"] for x in t.request("tools/list", {}, timeout=10)["tools"]] == ["echo", "add"]
        r = t.request("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, timeout=10, name="add")
        assert r["content"][0]["text"] == "5"
        ids = [p["body"]["id"] for p in s.posts() if p["body"].get("id") is not None]
        assert len(ids) == len(set(ids)) == 3, ids
        assert all(p["path"].startswith("/messages?session=sse-1") for p in s.posts())
    finally:
        t.close()


def test_an_absolute_endpoint_is_used_as_given(stub):
    s = stub("--sse-absolute-endpoint")
    t = transport(s.url)
    t.start()
    try:
        assert t.request("tools/call", {"name": "echo", "arguments": {"text": "abs"}}, timeout=10, name="echo")
        assert all(p["path"].startswith("/messages?session=") for p in s.posts())
    finally:
        t.close()


def test_auto_falls_back_in_spec_order(stub):
    s = stub()
    t = transport(s.url, kind="auto")
    t.start()
    try:
        assert t.era == "sse"
        rows = [r for r in s.rows() if r["http"] in ("GET", "POST")]
        first = [(r["http"], (r.get("body") or {}).get("method") if isinstance(r.get("body"), dict) else None)
                 for r in rows[:3]]
        assert first == [("POST", "server/discover"), ("POST", "initialize"), ("GET", None)], first
        assert rows[0]["status"] in (400, 404, 405) and rows[1]["status"] in (400, 404, 405)
        assert len(s.gets()) == 1
    finally:
        t.close()


def test_the_bearer_is_on_every_request_and_401_is_auth(stub):
    s = stub("--require-bearer", "tok-123")
    t = transport(s.url, headers={"Authorization": "Bearer tok-123"})
    t.start()
    try:
        t.request("tools/list", {}, timeout=10)
        assert all(r["headers"].get("authorization") == "Bearer tok-123" for r in s.rows()), s.rows()
    finally:
        t.close()
    bad = transport(s.url)
    with pytest.raises(tg_http.TransportError) as ei:
        bad.start()
    assert ei.value.kind == "auth" and ei.value.status == 401
    bad.close()


def test_a_dropped_stream_reconnects_once_and_reissues(stub):
    s = stub("--sse-drop-after", "2")        # the stream closes after the initialize and tools/list replies
    t = transport(s.url)
    t.start()
    try:
        t.request("tools/list", {}, timeout=10)
        r = t.request("tools/call", {"name": "echo", "arguments": {"text": "again"}}, timeout=10, name="echo")
        assert "again" in json.dumps(r)
        assert len(s.gets()) == 2 and len(s.posts("initialize")) == 2, "one reconnect: a new stream, a new init"
        calls = s.posts("tools/call")
        assert calls[-1]["path"].startswith("/messages?session=sse-2")
        assert len({c["body"]["id"] for c in calls}) == len(calls), "re-issued with a new id"
    finally:
        t.close()


def test_a_second_drop_raises(stub):
    s = stub("--sse-drop-after", "1")        # every stream dies after one reply
    t = transport(s.url)
    t.start()
    try:
        with pytest.raises(tg_http.TransportError):
            t.request("tools/list", {}, timeout=10)
        assert len(s.gets()) <= 3, "one reconnect, not a loop"
    finally:
        t.close()


def test_a_reply_that_never_comes_is_a_timeout(stub):
    s = stub("--slow", "4")
    t = transport(s.url, call_timeout=1.0)
    t.start()
    try:
        t0 = time.monotonic()
        with pytest.raises(tg_http.TransportError) as ei:
            t.request("tools/call", {"name": "echo", "arguments": {"text": "x"}}, timeout=1.0, name="echo")
        assert ei.value.kind == "timeout" and time.monotonic() - t0 < 8
    finally:
        t.close()


def test_close_sends_no_delete(stub):
    s = stub()
    t = transport(s.url)
    t.start()
    t.close()
    time.sleep(0.3)
    assert [r for r in s.rows() if r["http"] == "DELETE"] == []


# ---- the backend and the CLI ---------------------------------------------------------------------------
def write_cfg(tmp_path, servers):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return str(p)


def test_a_type_sse_backend_is_live(stub, tmp_path, monkeypatch):
    s = stub()
    monkeypatch.setenv("TOK_A1B", "tok-9")
    r = tg.Router(write_cfg(tmp_path, {"old": {"url": s.url, "type": "sse",
                                               "headers": {"X-Api-Key": "${TOK_A1B}"}}}))
    r.start()
    try:
        b = r.backends["old"]
        assert b.status == "ok", (b.status, b.error)
        assert [t["name"] for t in b.tools] == ["echo", "add"]
        assert "via sse" in json.dumps(b.call("echo", {"text": "via sse"}))
        assert all(x["headers"].get("x-api-key") is None or x["headers"]["x-api-key"] == "tok-9" for x in s.rows())
    finally:
        for b in r.backends.values():
            b.stop()


def test_selftest_prints_the_sse_line(stub, tmp_path):
    s = stub()
    cfg = write_cfg(tmp_path, {"old": {"url": s.url, "type": "sse"}})
    env = dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1")
    p = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--selftest", "--config", cfg],
                       capture_output=True, text=True, timeout=120, env=env, encoding="utf-8", errors="replace")
    assert "[remote] old: sse 2024-11-05 2 tools" in p.stdout.splitlines(), p.stdout[-1500:] + p.stderr[-800:]
    assert p.returncode == 0


def test_nothing_says_sse_lands_later():
    for f in ("tool_guardian.py", "tg_http.py", "README.md"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert "SSE lands in 0.9.1" not in text and "until 0.9.1" not in text, f
