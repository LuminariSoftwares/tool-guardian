"""A stdlib MCP-over-HTTP stub for tool-guardian's remote backends (P110 C3).

It speaks one of three spec generations, chosen by --era:

  modern  2026-07-28, stateless: server/discover, `_meta` on every request, Mcp-Method / Mcp-Name /
          MCP-Protocol-Version validated (-32020), an unsupported version answered with -32022 and
          `data.supported`, an unknown method with 404 -32601, GET and DELETE with 405.
  legacy  2025-11-25 / 2025-06-18 / 2025-03-26 (--version), stateful: initialize, Mcp-Session-Id,
          a dropped session answered 404, DELETE ends a session.
  sse     2024-11-05 HTTP+SSE (deprecated upstream): GET opens a stream whose first event is `endpoint`;
          requests are POSTed there and answered on the stream.

The first stdout line is `STUB READY port=<p> proxy_port=<pp>` (pp 0 without --proxy-port). Every request
is appended to --log as one JSON line, so a test can assert what the client sent. Two tools: `echo` and
`add`. Loopback only; no third-party imports; Python 3.9+.

Run: python tests/http_stub_server.py --era legacy --version 2025-06-18 --log stub.jsonl
"""
import argparse
import base64
import json
import queue
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SSE_VERSION = "2024-11-05"
TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]
NAMED = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
LOG_HEADERS = ("accept", "content-type", "authorization", "mcp-method", "mcp-name", "mcp-protocol-version",
               "mcp-session-id", "last-event-id")


def decode_name(value):
    """Mcp-Name as sent: plain, or the `=?base64?<b64>?=` sentinel for values that are not header-safe."""
    if value and value.startswith("=?base64?") and value.endswith("?="):
        return base64.b64decode(value[len("=?base64?"):-2]).decode("utf-8")
    return value


class State:
    def __init__(self, opts):
        self.opts = opts
        self.lock = threading.Lock()
        self.sessions = {}          # legacy: session id -> requests served since initialize
        self.n_sessions = 0
        self.version_rejected = False
        self.input_asked = False
        self.primed = set()
        self.sse_streams = {}       # sse era: session -> queue of events
        self.port = 0

    def log(self, rec):
        if not self.opts.log:
            return
        with self.lock, open(self.opts.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")


def tool_result(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "echo":
        text = str(args.get("text", ""))
    elif name == "add":
        text = str(args.get("a", 0) + args.get("b", 0))
    else:
        return None
    return {"content": [{"type": "text", "text": text}], "isError": False}


def make_handler(st):
    o = st.opts

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        # ---- plumbing -------------------------------------------------------------------------------
        def _record(self, body, status):
            hd = {k: self.headers.get(k) for k in LOG_HEADERS if self.headers.get(k) is not None}
            st.log({"http": self.command, "path": self.path, "headers": hd, "body": body, "status": status,
                    "ts": time.time()})

        def _send(self, status, obj=None, ctype="application/json", extra=None, body=None, raw=None):
            self._record(body, status)
            data = b"" if obj is None and raw is None else (raw if raw is not None else json.dumps(obj).encode())
            if obj is not None and ctype == "text/event-stream":
                data = b"event: message\r\ndata: " + json.dumps(obj).encode() + b"\r\n\r\n"
            self.send_response(status)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if data:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _err(self, status, rid, code, message, data=None, body=None):
            e = {"code": code, "message": message}
            if data is not None:
                e["data"] = data
            self._send(status, {"jsonrpc": "2.0", "id": rid, "error": e}, body=body)

        def _ok(self, rid, result, body, extra=None):
            ctype = "text/event-stream" if o.sse_responses else "application/json"
            self._send(200, {"jsonrpc": "2.0", "id": rid, "result": result}, ctype=ctype, extra=extra, body=body)

        def _auth_ok(self, body):
            if not o.require_bearer:
                return True
            if self.headers.get("Authorization") == "Bearer " + o.require_bearer:
                return True
            meta = "http://127.0.0.1:%d/.well-known/oauth-protected-resource" % st.port
            self._send(401, {"error": "unauthorized"}, body=body,
                       extra={"WWW-Authenticate": 'Bearer resource_metadata="%s"' % meta})
            return False

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                return {"_unparsed": raw.decode("utf-8", "replace")}

        # ---- verbs ----------------------------------------------------------------------------------
        def do_GET(self):
            if o.era == "sse" and urlparse(self.path).path == o.path:
                return self._sse_stream()
            self._send(405, body=None)

        def do_DELETE(self):
            if o.era == "legacy":
                sid = self.headers.get("Mcp-Session-Id")
                if o.delete_405:
                    return self._send(405)
                with st.lock:
                    st.sessions.pop(sid, None)
                return self._send(200)
            self._send(405)

        def do_POST(self):
            body = self._read_body()
            if o.slow and isinstance(body, dict) and body.get("method") == "tools/call":
                time.sleep(o.slow)
            if not self._auth_ok(body):
                return None
            if o.era == "modern":
                return self._modern(body)
            if o.era == "legacy":
                return self._legacy(body)
            return self._sse_post(body)

        # ---- modern 2026-07-28 ----------------------------------------------------------------------
        def _modern(self, body):
            if not isinstance(body, dict) or "method" not in body:
                return self._err(400, None, -32600, "Invalid Request", body=body)
            rid = body.get("id")
            method = body["method"]
            params = body.get("params") or {}
            meta = params.get("_meta") or {}
            if rid is None:  # notification
                return self._send(202, body=body)
            if self.headers.get("Mcp-Method") != method:
                return self._err(400, rid, -32020, "HeaderMismatch: Mcp-Method", body=body)
            if method in NAMED and decode_name(self.headers.get("Mcp-Name")) != params.get(NAMED[method]):
                return self._err(400, rid, -32020, "HeaderMismatch: Mcp-Name", body=body)
            if o.force_header_mismatch and method == "tools/call":
                return self._err(400, rid, -32020, "HeaderMismatch: forced", body=body)
            if method not in ("server/discover", "tools/list", "tools/call"):
                return self._err(404, rid, -32601, "Method not found: " + method, body=body)
            version = meta.get("io.modelcontextprotocol/protocolVersion")
            supported = o.supported.split(",")
            if o.reject_version_once and not st.version_rejected:
                st.version_rejected = True
                return self._err(400, rid, -32022, "UnsupportedProtocolVersionError", {"supported": supported},
                                 body=body)
            if version not in supported:
                return self._err(400, rid, -32022, "UnsupportedProtocolVersionError", {"supported": supported},
                                 body=body)
            if self.headers.get("MCP-Protocol-Version") != version:
                return self._err(400, rid, -32020, "HeaderMismatch: MCP-Protocol-Version", body=body)
            if "io.modelcontextprotocol/clientCapabilities" not in meta:
                return self._err(400, rid, -32602, "missing _meta clientCapabilities", body=body)
            rt = {} if o.omit_result_type else {"resultType": "complete"}
            if method == "server/discover":
                res = dict(rt, supportedVersions=supported, capabilities={"tools": {}},
                           serverInfo={"name": "stub-modern", "version": "1.0.0"})
                return self._ok(rid, res, body)
            if method == "tools/list":
                return self._ok(rid, dict(rt, tools=TOOLS), body)
            if o.input_required and not params.get("inputResponses") and not st.input_asked:
                st.input_asked = True
                ask = {"confirm": {"method": "elicitation/create",
                                   "params": {"message": "Run it?", "requestedSchema": {
                                       "type": "object", "properties": {"ok": {"type": "boolean"}}}}}}
                return self._ok(rid, {"resultType": "input_required", "inputRequests": ask}, body)
            res = tool_result(params)
            if res is None:
                return self._err(200, rid, -32602, "unknown tool", body=body)
            return self._ok(rid, dict(rt, **res), body)

        # ---- legacy 2025-11-25 / 06-18 / 03-26 ------------------------------------------------------
        def _legacy(self, body):
            if not isinstance(body, dict) or "method" not in body:
                return self._err(400, None, -32600, "Invalid Request", body=body)
            rid = body.get("id")
            method = body["method"]
            hdr_ver = self.headers.get("MCP-Protocol-Version")
            if method != "initialize" and hdr_ver and hdr_ver not in LEGACY:
                if o.reject_shape == "empty":
                    return self._send(400, body=body)
                # the shape the TypeScript SDK (and DeepWiki, probed 2026-10-30) answers a modern request with
                return self._send(400, {"jsonrpc": "2.0", "id": "server-error", "error": {
                    "code": -32600, "message": "Bad Request: Unsupported protocol version: %s. Supported versions: "
                                               "2024-11-05, %s" % (hdr_ver, ", ".join(reversed(LEGACY)))}},
                    body=body)
            if method == "initialize":
                offered = (body.get("params") or {}).get("protocolVersion")
                mine = [v for v in LEGACY if v <= o.version]
                version = offered if offered in mine else o.version
                extra = {}
                if not o.no_session:
                    with st.lock:
                        st.n_sessions += 1
                        sid = "stub-session-%d" % st.n_sessions
                        st.sessions[sid] = 0
                    extra["Mcp-Session-Id"] = sid
                res = {"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "stub-legacy", "version": "1.0.0"}}
                return self._ok(rid, res, body, extra=extra)
            if not o.no_session:
                sid = self.headers.get("Mcp-Session-Id")
                if not sid:
                    return self._err(400, rid, -32000, "Bad Request: No valid session ID provided", body=body)
                with st.lock:  # only requests (with an id) count toward --expire-after; notifications do not
                    served = st.sessions.get(sid)
                    if served is not None and rid is not None:
                        if o.expire_after is not None and served >= o.expire_after:
                            st.sessions.pop(sid, None)
                            served = None
                        else:
                            st.sessions[sid] = served + 1
                if served is None:
                    return self._err(404, rid, -32001, "Session not found", body=body)
            if rid is None:
                return self._send(202, body=body)
            if method == "tools/list":
                if o.prime_and_close and rid not in st.primed and not st.primed:
                    st.primed.add(rid)
                    self._record(body, 200)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b"id: prime-1\r\ndata: \r\n\r\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return None
                return self._ok(rid, {"tools": TOOLS}, body)
            if method == "tools/call":
                res = tool_result(body.get("params") or {})
                if res is None:
                    return self._err(200, rid, -32602, "unknown tool", body=body)
                return self._ok(rid, res, body)
            return self._err(200, rid, -32601, "Method not found: " + method, body=body)

        # ---- 2024-11-05 HTTP+SSE --------------------------------------------------------------------
        def _sse_stream(self):
            with st.lock:
                st.n_sessions += 1
                sid = "sse-%d" % st.n_sessions
                q = queue.Queue()
                st.sse_streams[sid] = q
            self._record(None, 200)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            where = ("http://127.0.0.1:%d" % st.port if o.sse_absolute_endpoint else "") + "/messages?session=" + sid
            self.wfile.write(("event: endpoint\r\ndata: %s\r\n\r\n" % where).encode())
            self.wfile.flush()
            sent = 0
            try:
                while o.sse_drop_after is None or sent < o.sse_drop_after:
                    try:
                        obj = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\r\n\r\n")
                        self.wfile.flush()
                        continue
                    if obj is None:
                        break
                    self.wfile.write(b"event: message\r\ndata: " + json.dumps(obj).encode() + b"\r\n\r\n")
                    self.wfile.flush()
                    sent += 1
            except OSError:
                pass
            with st.lock:
                st.sse_streams.pop(sid, None)     # a POST to a closed session is answered 404
            self.close_connection = True

        def _sse_post(self, body):
            u = urlparse(self.path)
            if u.path != "/messages":
                return self._send(405, body=body)
            sid = (parse_qs(u.query).get("session") or [""])[0]
            q = st.sse_streams.get(sid)
            if q is None:
                return self._send(404, body=body)
            self._send(202, body=body)
            if not isinstance(body, dict) or body.get("id") is None:
                return None
            rid, method = body.get("id"), body.get("method")
            if method == "initialize":
                res = {"protocolVersion": SSE_VERSION, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "stub-sse", "version": "1.0.0"}}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                res = tool_result(body.get("params") or {})
            else:
                res = None
            if res is None:
                q.put({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
            else:
                q.put({"jsonrpc": "2.0", "id": rid, "result": res})
            return None

    return H


class LoopbackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in server_bind: HTTPServer.server_bind calls
    socket.getfqdn(host) on every start, which took ~25 s per start on macOS CI runners (P110 S1: the macOS
    pytest job went from 71 s to 1,024 s). The stub only ever binds 127.0.0.1, so the name is known."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class Proxy(socketserver.ThreadingTCPServer):
    """CONNECT-only stub proxy: records the tunnel request line, then refuses it with 502."""
    daemon_threads = True
    allow_reuse_address = True


def make_proxy_handler(st):
    class P(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline(4096).decode("latin-1").strip()
            while self.rfile.readline(4096) not in (b"\r\n", b"\n", b""):
                pass
            st.log({"proxy": line, "ts": time.time()})
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

    return P


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--era", choices=("modern", "legacy", "sse"), required=True)
    ap.add_argument("--version", choices=LEGACY, default=LEGACY[0], help="legacy era: newest version spoken")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--path", default="/mcp")
    ap.add_argument("--log", default="")
    ap.add_argument("--require-bearer", default="", metavar="TOKEN")
    ap.add_argument("--slow", type=float, default=0.0, metavar="S", help="sleep S seconds before tools/call")
    ap.add_argument("--supported", default=MODERN, help="modern era: comma list of versions it accepts")
    ap.add_argument("--reject-version-once", action="store_true", help="modern: answer the first request -32022")
    ap.add_argument("--force-header-mismatch", action="store_true", help="modern: tools/call -> -32020")
    ap.add_argument("--input-required", action="store_true", help="modern: first tools/call -> input_required")
    ap.add_argument("--omit-result-type", action="store_true", help="modern: results without resultType")
    ap.add_argument("--expire-after", type=int, default=None, metavar="N",
                    help="legacy: a session serves N requests after initialize, then 404")
    ap.add_argument("--sse-responses", action="store_true", help="answer requests as an SSE stream")
    ap.add_argument("--prime-and-close", action="store_true",
                    help="legacy 2025-11-25: the first tools/list gets a priming event, then the stream closes")
    ap.add_argument("--no-session", action="store_true", help="legacy: no Mcp-Session-Id (stateless legacy)")
    ap.add_argument("--reject-shape", choices=("tssdk", "empty"), default="tssdk",
                    help="legacy: how a modern request is refused (TypeScript-SDK -32600 body, or empty 400)")
    ap.add_argument("--delete-405", action="store_true", help="legacy: refuse DELETE with 405")
    ap.add_argument("--sse-drop-after", type=int, default=None, metavar="N",
                    help="sse: close the stream after N message events and forget the session (POSTs to it -> 404)")
    ap.add_argument("--sse-absolute-endpoint", action="store_true",
                    help="sse: send the endpoint event as an absolute URL instead of a path")
    ap.add_argument("--proxy-port", type=int, default=None, metavar="P",
                    help="also run a CONNECT-only stub proxy on P (0 = any free port)")
    return ap


def main(argv=None):
    opts = build_parser().parse_args(argv)
    st = State(opts)
    srv = LoopbackHTTPServer(("127.0.0.1", opts.port), make_handler(st))
    srv.daemon_threads = True
    st.port = srv.server_address[1]
    pport = 0
    if opts.proxy_port is not None:
        px = Proxy(("127.0.0.1", opts.proxy_port), make_proxy_handler(st))
        pport = px.server_address[1]
        threading.Thread(target=px.serve_forever, daemon=True).start()
    print("STUB READY port=%d proxy_port=%d" % (st.port, pport), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def free_port():
    """A free loopback port (for callers that need one before the stub starts)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    sys.exit(main())
