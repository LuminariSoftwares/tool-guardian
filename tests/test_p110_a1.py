"""P110 S1 acceptance (A1): remote MCP backends over Streamable HTTP, modern 2026-07-28 and legacy
2025-11-25 / 2025-06-18 / 2025-03-26, detected per origin (plan P110 decisions 1-5, contracts C1-C3).

Written by the overseer from the contract, RED on main before the PR: `tg_http` does not exist yet and a
`url` backend is reported UNSUPPORTED. Every case runs against tests/http_stub_server.py, never the network.

One real-world case is pinned here because the contract's short form missed it: DeepWiki (probed
2026-10-30) refuses a modern `server/discover` with HTTP 400 and JSON-RPC error -32600 "Unsupported protocol
version". The spec's rule is "a recognized modern JSON-RPC error (-32022, -32020, 404 + -32601) means modern;
anything else means initialize", so that body is LEGACY.
"""
import base64
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
try:
    import tg_http
except ImportError:  # RED on main: every test below fails on this, none is skipped
    tg_http = None

MODERN = "2026-07-28"
DEEPWIKI_400 = (b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Unsupported '
                b'protocol version: 2026-07-28. Supported versions: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25"}}')


def mod():
    assert tg_http is not None, "tg_http.py is missing (P110 C1)"
    return tg_http


class Stub:
    def __init__(self, tmp_path, *args):
        self.log = tmp_path / ("stub_%d.jsonl" % time.monotonic_ns())
        self.proc = subprocess.Popen([sys.executable, str(STUB), "--log", str(self.log), *args],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().strip()
        assert line.startswith("STUB READY"), line + self.proc.stderr.read()
        kv = dict(part.split("=") for part in line.split()[2:])
        self.port, self.proxy_port = int(kv["port"]), int(kv["proxy_port"])
        self.url = "http://127.0.0.1:%d/mcp" % self.port

    def rows(self):
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def posts(self, method):
        return [r for r in self.rows() if r.get("http") == "POST" and isinstance(r.get("body"), dict)
                and r["body"].get("method") == method]

    def stop(self):
        self.proc.kill()
        self.proc.communicate(timeout=10)


@pytest.fixture
def stub(tmp_path):
    made = []

    def start(*args):
        s = Stub(tmp_path, *args)
        made.append(s)
        return s

    yield start
    for s in made:
        s.stop()


def transport(url, **kw):
    kw.setdefault("start_timeout", 10.0)
    kw.setdefault("call_timeout", 10.0)
    return mod().HttpTransport(url, kw.pop("headers", {}), **kw)


def err_body(code, data=None):
    e = {"code": code, "message": "x"}
    if data is not None:
        e["data"] = data
    return json.dumps({"jsonrpc": "2.0", "id": 1, "error": e}).encode()


# ---- pure functions ---------------------------------------------------------------------------------------
def test_detect_era_recognises_modern_errors():
    d = mod().detect_era
    assert d(400, err_body(-32022, {"supported": [MODERN]})) == "modern"
    assert d(400, err_body(-32020)) == "modern"
    assert d(404, err_body(-32601)) == "modern"


def test_detect_era_everything_else_is_legacy():
    d = mod().detect_era
    assert d(400, b"") == "legacy"
    assert d(400, b"<html><body>Bad Request</body></html>") == "legacy"
    assert d(405, b"") == "legacy"
    assert d(404, b"") == "legacy"
    assert d(400, err_body(-32601)) == "legacy", "-32601 means modern only with a 404"
    assert d(400, DEEPWIKI_400) == "legacy", "DeepWiki's -32600 refusal is a legacy server"


def test_parse_sse_events():
    lines = ["event: message\r\n", "id: 7\r\n", "retry: 500\r\n", ": keep-alive\r\n", 'data: {"a":\r\n',
             'data: 1}\r\n', "\r\n", 'data: {"id": 2}\n', "\n"]
    events = list(mod().parse_sse(iter(lines)))
    assert events[0]["event"] == "message" and events[0]["id"] == "7" and events[0]["retry"] == 500
    assert events[0]["data"] == '{"a":\n1}'
    assert events[1]["data"] == '{"id": 2}' and events[1].get("event", "message") == "message"
    assert len(events) == 2, "a comment line is not an event"


# ---- modern 2026-07-28 --------------------------------------------------------------------------------------
def test_modern_discover_sets_era_version_and_server_info(stub):
    s = stub("--era", "modern")
    t = transport(s.url)
    res = t.start()
    assert t.era == "modern" and t.protocol_version == MODERN
    assert t.server_info == {"name": "stub-modern", "version": "1.0.0"} and res["serverInfo"] == t.server_info
    assert len(s.posts("server/discover")) == 1 and t.session_id is None
    t.close()


def test_modern_every_request_carries_meta_and_headers(stub):
    s = stub("--era", "modern")
    t = transport(s.url)
    t.start()
    tools = t.request("tools/list", {}, timeout=10)["tools"]
    assert [x["name"] for x in tools] == ["echo", "add"]
    row = s.posts("tools/list")[-1]
    meta = row["body"]["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == MODERN
    assert "io.modelcontextprotocol/clientCapabilities" in meta
    h = row["headers"]
    assert h["mcp-method"] == "tools/list" and h["mcp-protocol-version"] == MODERN
    assert h["accept"] == "application/json, text/event-stream" and h["content-type"] == "application/json"
    assert "mcp-session-id" not in h
    t.close()


def test_modern_mcp_name_plain_and_base64_sentinel(stub):
    s = stub("--era", "modern")
    t = transport(s.url)
    t.start()
    r = t.request("tools/call", {"name": "echo", "arguments": {"text": "hi"}}, timeout=10, name="echo")
    assert r["content"][0]["text"] == "hi"
    assert s.posts("tools/call")[-1]["headers"]["mcp-name"] == "echo"
    with pytest.raises(mod().TransportError):  # the stub validates Mcp-Name against params.name
        t.request("tools/call", {"name": "echö", "arguments": {}}, timeout=10, name="echö")
    sent = s.posts("tools/call")[-1]["headers"]["mcp-name"]
    assert sent == "=?base64?" + base64.b64encode("echö".encode("utf-8")).decode("ascii") + "?="
    t.close()


def test_modern_unsupported_version_is_retried_once(stub):
    s = stub("--era", "modern", "--reject-version-once")
    t = transport(s.url)
    t.start()
    assert t.era == "modern" and t.protocol_version == MODERN
    assert len(s.posts("server/discover")) == 2, "one -32022, then one retry with a listed version"
    t.close()


def test_modern_header_mismatch_raises(stub):
    s = stub("--era", "modern", "--force-header-mismatch")
    t = transport(s.url)
    t.start()
    with pytest.raises(mod().TransportError) as ei:
        t.request("tools/call", {"name": "echo", "arguments": {"text": "x"}}, timeout=10, name="echo")
    assert ei.value.kind == "header_mismatch" and ei.value.status == 400
    t.close()


def test_modern_unknown_method_is_a_404_error(stub):
    s = stub("--era", "modern")
    t = transport(s.url)
    t.start()
    with pytest.raises(mod().TransportError) as ei:
        t.request("resources/list", {}, timeout=10)
    assert ei.value.status == 404
    t.close()


def test_modern_input_required_is_raised_with_the_result(stub):
    s = stub("--era", "modern", "--input-required")
    t = transport(s.url)
    t.start()
    with pytest.raises(mod().InputRequired) as ei:
        t.request("tools/call", {"name": "echo", "arguments": {"text": "x"}}, timeout=10, name="echo")
    assert ei.value.result["resultType"] == "input_required" and "confirm" in ei.value.result["inputRequests"]
    again = t.request("tools/call", {"name": "echo", "arguments": {"text": "x"},
                                     "inputResponses": {"confirm": {"action": "accept", "content": {"ok": True}}}},
                      timeout=10, name="echo")
    assert again["content"][0]["text"] == "x"
    t.close()


def test_modern_result_without_result_type_is_complete(stub):
    s = stub("--era", "modern", "--omit-result-type")
    t = transport(s.url)
    t.start()
    assert t.era == "modern"
    assert [x["name"] for x in t.request("tools/list", {}, timeout=10)["tools"]] == ["echo", "add"]
    t.close()


def test_modern_close_sends_no_delete(stub):
    s = stub("--era", "modern")
    t = transport(s.url)
    t.start()
    t.close()
    assert [r for r in s.rows() if r.get("http") == "DELETE"] == []


# ---- legacy 2025-11-25 / 2025-06-18 / 2025-03-26 -----------------------------------------------------------
@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26"])
def test_legacy_initialize_negotiates_each_version(stub, version):
    s = stub("--era", "legacy", "--version", version)
    t = transport(s.url)
    t.start()
    assert t.era == "legacy" and t.protocol_version == version
    assert t.server_info == {"name": "stub-legacy", "version": "1.0.0"}
    assert [x["name"] for x in t.request("tools/list", {}, timeout=10)["tools"]] == ["echo", "add"]
    assert s.posts("initialize")[0]["body"]["params"]["protocolVersion"] == "2025-11-25", "offers the newest first"
    assert s.posts("tools/list")[-1]["headers"]["mcp-protocol-version"] == version
    t.close()


def test_legacy_session_id_is_echoed_and_initialized_is_sent(stub):
    s = stub("--era", "legacy")
    t = transport(s.url)
    t.start()
    t.request("tools/list", {}, timeout=10)
    assert t.session_id == "stub-session-1"
    note = s.posts("notifications/initialized")
    assert len(note) == 1 and note[0]["headers"]["mcp-session-id"] == "stub-session-1" and note[0]["status"] == 202
    assert s.posts("tools/list")[-1]["headers"]["mcp-session-id"] == "stub-session-1"
    t.close()


def test_legacy_without_sessions_over_sse_like_deepwiki(stub):
    s = stub("--era", "legacy", "--no-session", "--sse-responses")
    t = transport(s.url)
    t.start()
    assert t.era == "legacy" and t.session_id is None and t.protocol_version == "2025-11-25"
    assert s.posts("server/discover")[0]["status"] == 400, "modern first, refused with the -32600 body"
    assert [x["name"] for x in t.request("tools/list", {}, timeout=10)["tools"]] == ["echo", "add"]
    t.close()


def test_legacy_after_a_plain_400(stub):
    s = stub("--era", "legacy", "--reject-shape", "empty")
    t = transport(s.url)
    t.start()
    assert t.era == "legacy" and len(s.posts("initialize")) == 1
    t.close()


def test_legacy_404_reinitializes_exactly_once(stub):
    s = stub("--era", "legacy", "--expire-after", "1")
    t = transport(s.url)
    t.start()
    t.request("tools/list", {}, timeout=10)
    r = t.request("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, timeout=10, name="add")
    assert r["content"][0]["text"] == "5"
    assert len(s.posts("initialize")) == 2 and t.session_id == "stub-session-2"
    t.close()


def test_legacy_second_404_raises_session_expired(stub):
    s = stub("--era", "legacy", "--expire-after", "0")
    t = transport(s.url)
    t.start()
    with pytest.raises(mod().TransportError) as ei:
        t.request("tools/list", {}, timeout=10)
    assert ei.value.kind == "session_expired" and ei.value.status == 404
    assert len(s.posts("initialize")) == 2, "one re-initialize, not a loop"
    t.close()


def test_legacy_prime_and_close_reissues_with_a_new_id(stub):
    s = stub("--era", "legacy", "--prime-and-close")
    t = transport(s.url)
    t.start()
    assert [x["name"] for x in t.request("tools/list", {}, timeout=10)["tools"]] == ["echo", "add"]
    ids = [r["body"]["id"] for r in s.posts("tools/list")]
    assert len(ids) == 2 and ids[0] != ids[1], "a broken stream is re-issued as a NEW request id"
    assert [r for r in s.rows() if r.get("http") == "GET"] == [], "no GET resume stream"
    t.close()


def test_legacy_close_deletes_the_session_and_ignores_405(stub):
    s = stub("--era", "legacy")
    t = transport(s.url)
    t.start()
    t.close()
    dels = [r for r in s.rows() if r.get("http") == "DELETE"]
    assert len(dels) == 1 and dels[0]["headers"]["mcp-session-id"] == "stub-session-1"
    s2 = stub("--era", "legacy", "--delete-405")
    t2 = transport(s2.url)
    t2.start()
    t2.close()  # must not raise


# ---- common: errors, auth, TLS rule, proxies, timeouts --------------------------------------------------------
def test_plaintext_http_to_a_non_loopback_host_is_refused():
    t = transport("http://192.0.2.10:8080/mcp", start_timeout=2.0)
    with pytest.raises(mod().TransportError) as ei:
        t.start()
    assert ei.value.kind == "tls" and ei.value.status == 0
    assert str(ei.value.detail) == "plaintext http to a non-loopback host; set allowInsecure true for this server"


def test_call_timeout_is_a_timeout_error(stub):
    s = stub("--era", "modern", "--slow", "3")
    t = transport(s.url)
    t.start()
    with pytest.raises(mod().TransportError) as ei:
        t.request("tools/call", {"name": "echo", "arguments": {"text": "x"}}, timeout=0.5, name="echo")
    assert ei.value.kind == "timeout"
    t.close()


def test_https_proxy_uses_a_connect_tunnel(stub, monkeypatch):
    s = stub("--era", "modern", "--proxy-port", "0")
    for k in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:%d" % s.proxy_port)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:%d" % s.proxy_port)
    t = transport("https://mcp.example.org/mcp", start_timeout=5.0)
    with pytest.raises(mod().TransportError) as ei:
        t.start()
    assert ei.value.kind == "connect" and "127.0.0.1" in str(ei.value.detail), "names the proxy that refused"
    lines = [r["proxy"] for r in s.rows() if "proxy" in r]
    assert lines and lines[0].startswith("CONNECT mcp.example.org:443 ")


def test_no_proxy_skips_the_tunnel(stub, monkeypatch):
    s = stub("--era", "modern", "--proxy-port", "0")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:%d" % s.proxy_port)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:%d" % s.proxy_port)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    t = transport(s.url)
    t.start()
    assert t.era == "modern"
    assert [r for r in s.rows() if "proxy" in r] == []
    t.close()


# ---- the Backend seam (C2) ----------------------------------------------------------------------------------
def write_cfg(tmp_path, servers):
    cfg = tmp_path / "remote.json"
    cfg.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return str(cfg)


def test_router_backend_bearer_from_env(stub, tmp_path, monkeypatch):
    s = stub("--era", "legacy", "--require-bearer", "tok-123")
    monkeypatch.setenv("TG_A1_TOKEN", "tok-123")
    r = tg.Router(write_cfg(tmp_path, {"stub": {"url": s.url, "headers": {"Authorization": "Bearer ${TG_A1_TOKEN}"}}}))
    r.start()
    try:
        b = r.backends["stub"]
        assert b.status == "ok", b.error
        assert [x["name"] for x in b.tools] == ["echo", "add"]
        assert s.posts("initialize")[-1]["headers"]["authorization"] == "Bearer tok-123"
    finally:
        for b in r.backends.values():
            b.stop()


def test_router_backend_401_is_auth_required(stub, tmp_path):
    s = stub("--era", "modern", "--require-bearer", "tok-123")
    r = tg.Router(write_cfg(tmp_path, {"stub": {"url": s.url}}))
    r.start()
    try:
        b = r.backends["stub"]
        assert b.status == "AUTH_REQUIRED"
        assert "http://127.0.0.1:%d/.well-known/oauth-protected-resource" % s.port in b.error
    finally:
        for b in r.backends.values():
            b.stop()


def test_router_non_http_url_is_still_unsupported(tmp_path):
    r = tg.Router(write_cfg(tmp_path, {"odd": {"url": "ftp://example.org/mcp"}}))
    r.start()
    assert r.backends["odd"].status == "UNSUPPORTED"


def test_router_calls_a_remote_tool(stub, tmp_path):
    s = stub("--era", "modern")
    r = tg.Router(write_cfg(tmp_path, {"stub": {"url": s.url}}))
    r.start()
    try:
        assert r.backends["stub"].status == "ok", r.backends["stub"].error
        out = r.backends["stub"].call("echo", {"text": "via router"})
        assert "via router" in json.dumps(out)
    finally:
        for b in r.backends.values():
            b.stop()


def test_selftest_prints_the_remote_line(stub, tmp_path):
    s = stub("--era", "modern")
    cfg = write_cfg(tmp_path, {"stub": {"url": s.url}})
    env = dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1")
    p = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--selftest", "--config", cfg],
                       capture_output=True, text=True, timeout=120, env=env, encoding="utf-8", errors="replace")
    assert "[remote] stub: modern 2026-07-28 2 tools" in p.stdout.splitlines(), p.stdout[-1500:] + p.stderr[-800:]
    assert p.returncode == 0
