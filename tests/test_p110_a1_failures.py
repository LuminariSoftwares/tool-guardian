"""P110 S1 review addendum (O5 2026-10-30): how a remote server's errors reach the Backend. DO NOT EDIT.

Plan P110 decision 5 and the README "Remote servers" section: a TRANSPORT failure takes the stdio path -- one
reconnect after the backoff, DOWN on a second failure inside RESPAWN_WINDOW. An HTTP 5xx is a transport failure
(the server, or a gateway in front of it, is not answering). A JSON-RPC error inside a 200 is a TOOL error: the
call fails, the backend stays ok and nothing is rescheduled -- exactly as a stdio server answering an error.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import tool_guardian as tg

TOOLS = [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object", "properties": {}}}]


def server(call_reply):
    """A modern 2026-07-28 server: discover and tools/list work; tools/call answers with call_reply()."""
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            rid, method = body.get("id"), body.get("method")
            if rid is None:
                status, out = 202, None
            elif method == "server/discover":
                status, out = 200, {"jsonrpc": "2.0", "id": rid, "result": {
                    "resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}},
                    "serverInfo": {"name": "s", "version": "1"}}}
            elif method == "tools/list":
                status, out = 200, {"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete",
                                                                            "tools": TOOLS}}
            else:
                status, out = call_reply(rid)
            data = b"" if out is None else json.dumps(out).encode()
            self.send_response(status)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_DELETE(self):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "_backoff_sleep", lambda s: None)
    made = []

    def make(call_reply):
        srv = server(call_reply)
        made.append(srv)
        b = tg.Backend("remote", {"url": "http://127.0.0.1:%d/mcp" % srv.server_address[1]}, stats={})
        b.start()
        assert b.status == "ok", b.error
        return b

    yield make
    for s in made:
        s.shutdown()
        s.server_close()


def test_a_5xx_is_a_transport_failure_reconnect_then_down(backend):
    b = backend(lambda rid: (503, None))
    with pytest.raises(tg.BackendExited):
        b.call("echo", {})
    assert b._respawn_due is not None, "the first 5xx schedules one reconnect"
    with pytest.raises(tg.BackendExited):
        b.call("echo", {})
    assert b.respawns == 1 and b.stats.get("backend_respawns") == 1
    assert b.status == "DOWN", "a second transport failure inside RESPAWN_WINDOW marks it DOWN"


def test_a_json_rpc_error_in_a_200_is_a_tool_error_only(backend):
    b = backend(lambda rid: (200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "bad args"}}))
    for _ in range(3):
        with pytest.raises(RuntimeError) as ei:
            b.call("echo", {})
        assert not isinstance(ei.value, tg.BackendExited) and "bad args" in str(ei.value)
    assert b.status == "ok" and b._respawn_due is None and b.respawns == 0
