"""Streamable HTTP client for remote MCP servers (stdlib only).

Two protocol generations, told apart once per origin:
  modern  2026-07-28: stateless. First contact is `server/discover`; every request carries `_meta` and the
          Mcp-Method / Mcp-Name headers. Nothing to tear down.
  legacy  2025-11-25 / 2025-06-18 / 2025-03-26: `initialize`, an optional Mcp-Session-Id, a DELETE on close.
Not spoken: OAuth (a 401 is reported with the server's metadata URL), resumability, the GET stream,
`subscriptions/listen`, 2024-11-05 HTTP+SSE.

One POST per JSON-RPC message; the reply is JSON or a request-scoped SSE stream read up to the event whose id
matches. HTTPS always verifies the certificate; plain http is for loopback unless `allow_insecure`.
"""
from __future__ import annotations

import base64
import http.client
import itertools
import json
import re
import socket
import ssl
import time
import urllib.request
from urllib.parse import unquote, urlsplit

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
KINDS = ("tls", "connect", "proxy", "timeout", "http", "auth", "session_expired", "unsupported_version",
         "header_mismatch", "sse_parse")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
PLAINTEXT = "plaintext http to a non-loopback host; set allowInsecure true for this server"
NAMED = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
META = "io.modelcontextprotocol/"
MAX_BODY = 32 << 20
RESENDS = 2          # a stream that ends before its reply is re-issued this many times, each with a new id
CLOSE_TIMEOUT = 2.0


class TransportError(Exception):
    """A request that did not get an answer. `kind` is one of KINDS; `status` the HTTP status (0 if none)."""

    def __init__(self, kind, status=0, detail=""):
        self.kind, self.status, self.detail = kind, status, detail
        super().__init__("%s%s: %s" % (kind, " (HTTP %d)" % status if status else "", detail))


class InputRequired(Exception):
    """A modern result with resultType input_required; `.result` carries the inputRequests."""

    def __init__(self, result):
        self.result = result
        super().__init__("the server asked for input: %s" % ", ".join(sorted(result.get("inputRequests") or {})))


class _StreamEnded(Exception):
    pass


def parse_sse(lines_iter):
    """Server-sent events from an iterable of lines (str or bytes, LF or CRLF). Yields dicts with `data`
    (multi-line joined by \\n) and, when sent, `event`, `id` (str) and `retry` (int). Comments are skipped; an
    event without a `data` field is dropped, as is a final event the stream cut off before its blank line."""
    ev, data = {}, []
    for raw in lines_iter:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                yield dict(ev, data="\n".join(data))
            ev, data = {}, []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data":
                data.append(value)
            elif field in ("event", "id"):
                ev[field] = value
            elif field == "retry" and value.isdigit():
                ev["retry"] = int(value)


def _rpc_error(body):
    """(code, data) of the JSON-RPC error in a response body, or (None, None)."""
    try:
        err = json.loads(body.decode("utf-8", "replace") if isinstance(body, bytes) else body)["error"]
        return err["code"], err.get("data")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, None


def _supported(data):
    sup = data.get("supported") if isinstance(data, dict) else None
    return [v for v in sup if isinstance(v, str)] if isinstance(sup, list) else None


def detect_era(status, body):
    """Which protocol an answer to `server/discover` shows. Only a recognised modern error means modern:
    -32022 (unless it lists legacy versions only), -32020, or 404 with -32601. Everything else -- any other
    400/404/405, an empty or HTML body, any other JSON-RPC error code -- is a legacy server."""
    code, data = _rpc_error(body)
    if code == -32022:
        sup = _supported(data)
        return "modern" if sup is None or MODERN in sup else "legacy"
    if code == -32020 or (status == 404 and code == -32601):
        return "modern"
    return "legacy"


def _header_value(text):
    """A header value that is not printable ASCII travels as =?base64?<base64 of utf-8>?=."""
    plain = all(" " <= c <= "~" for c in text) and not (text.startswith("=?base64?") and text.endswith("?="))
    if plain:
        return text
    return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


class _Reply:
    def __init__(self, status, headers, msg, body):
        self.status, self.headers, self.msg, self.body = status, headers, msg, body


class HttpTransport:
    def __init__(self, url, headers=None, kind="auto", allow_insecure=False, start_timeout=90.0,
                 call_timeout=120.0, proxies=None, client_info=None):
        self.url, self.headers = url, dict(headers or {})
        self.kind = kind                       # "auto" only until HTTP+SSE lands; "type" in the config maps here
        self.allow_insecure = allow_insecure
        self.start_timeout, self.call_timeout = start_timeout, call_timeout
        self.proxies = proxies
        self.client_info = client_info or {"name": "tool-guardian", "version": "0"}
        self.era = self.protocol_version = self.session_id = self.server_info = None
        self._ids = itertools.count(1)
        self._parts = urlsplit(url)

    # ---- connection ----------------------------------------------------------------------------------
    def _proxy_for(self, host):
        scheme = self._parts.scheme
        if host in LOOPBACK:                  # a proxy cannot reach the caller's own loopback
            return None
        proxies = self.proxies if self.proxies is not None else urllib.request.getproxies()
        proxy = proxies.get(scheme)
        if not proxy or urllib.request.proxy_bypass(host):
            return None
        return urlsplit(proxy if "//" in proxy else "http://" + proxy)

    def _open(self, timeout):
        """(connection, request target, proxy name or None, extra headers); one connection per message."""
        p = self._parts
        host, https = p.hostname or "", p.scheme == "https"
        if p.scheme not in ("http", "https") or not host:
            raise TransportError("connect", 0, "not an http(s) URL")
        if not https and host not in LOOPBACK and not self.allow_insecure:
            raise TransportError("tls", 0, PLAINTEXT)
        port = p.port or (443 if https else 80)
        target = (p.path or "/") + ("?" + p.query if p.query else "")
        proxy = self._proxy_for(host)
        timeout = max(timeout, 0.001)
        if proxy is None:
            if https:
                ctx = ssl.create_default_context()
                return http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx), target, None, {}
            return http.client.HTTPConnection(host, port, timeout=timeout), target, None, {}
        pname = "%s:%d" % (proxy.hostname, proxy.port or 80)
        auth = {}
        if proxy.username:
            cred = "%s:%s" % (unquote(proxy.username), unquote(proxy.password or ""))
            auth["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode("ascii")
        if https:
            conn = http.client.HTTPSConnection(proxy.hostname, proxy.port or 80, timeout=timeout,
                                               context=ssl.create_default_context())
            conn.set_tunnel(host, port, headers=auth)
            return conn, target, pname, {}
        return http.client.HTTPConnection(proxy.hostname, proxy.port or 80, timeout=timeout), self.url, pname, auth

    # ---- one exchange --------------------------------------------------------------------------------
    def _headers(self, method, params, name, era, version):
        mine = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                "MCP-Protocol-Version": version}
        if era == "modern":
            mine["Mcp-Method"] = method
            if method in NAMED:
                mine["Mcp-Name"] = _header_value(str(name or (params or {}).get(NAMED[method]) or ""))
        elif self.session_id:
            mine["Mcp-Session-Id"] = self.session_id
        taken = {k.lower() for k in mine}
        out = {k: str(v) for k, v in self.headers.items() if k.lower() not in taken}
        out.update(mine)
        return out

    def _exchange(self, method, params, name, rid, deadline, era, version):
        body = {"jsonrpc": "2.0", "method": method}
        if rid is not None:
            body["id"] = rid
        if era == "modern":
            params = dict(params or {})
            params["_meta"] = dict(params.get("_meta") or {}, **{
                META + "protocolVersion": version, META + "clientCapabilities": {},
                META + "clientInfo": self.client_info})
        if params is not None:
            body["params"] = params
        hdrs = self._headers(method, params, name, era, version)
        return self._send("POST", json.dumps(body).encode("utf-8"), hdrs, rid, deadline)

    def _send(self, verb, data, hdrs, rid, deadline):
        conn, pname = None, None
        try:
            conn, target, pname, extra = self._open(deadline - time.monotonic())
            conn.request(verb, target, body=data, headers=dict(hdrs, **extra))
            resp = conn.getresponse()
            status = resp.status
            rh = {k.lower(): v for k, v in resp.getheaders()}
            ctype = rh.get("content-type", "").split(";")[0].strip().lower()
            if 200 <= status < 300 and ctype == "text/event-stream" and rid is not None:
                msg = self._read_stream(resp, conn, rid, deadline, status)
                return _Reply(status, rh, msg, json.dumps(msg).encode("utf-8"))
            raw = self._read_body(resp, conn, deadline, status)
            msg = None
            if 200 <= status < 300 and rid is not None and raw.strip():
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    parsed = None
                for m in parsed if isinstance(parsed, list) else [parsed]:
                    if isinstance(m, dict) and m.get("id") == rid and ("result" in m or "error" in m):
                        msg = m
            return _Reply(status, rh, msg, raw)
        except TransportError:
            raise
        except (socket.timeout, TimeoutError):
            raise TransportError("timeout", 0, "no reply within the time allowed") from None
        except ssl.SSLError as exc:
            if "timed out" in str(exc):
                raise TransportError("timeout", 0, "no reply within the time allowed") from None
            raise TransportError("tls", 0, "%s: %s" % (type(exc).__name__, exc)) from None
        except (OSError, http.client.HTTPException) as exc:
            if pname and str(exc).startswith("Tunnel connection failed"):
                kind = "proxy" if "407" in str(exc) else "connect"
                raise TransportError(kind, 0, "proxy %s refused the tunnel to %s: %s"
                                     % (pname, self._parts.netloc, exc)) from None
            if pname and isinstance(exc, ConnectionError):
                raise TransportError("proxy", 0, "could not reach proxy %s: %s" % (pname, exc)) from None
            raise TransportError("connect", 0, "%s: %s" % (type(exc).__name__, exc)) from None
        finally:
            if conn is not None:
                conn.close()

    def _read_body(self, resp, conn, deadline, status):
        chunks, size = [], 0
        while True:
            self._arm(conn, deadline)
            chunk = resp.read(65536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > MAX_BODY:
                raise TransportError("http", status, "response larger than %d bytes" % MAX_BODY)
            chunks.append(chunk)

    @staticmethod
    def _arm(conn, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TransportError("timeout", 0, "no reply within the time allowed")
        if conn.sock is not None:
            conn.sock.settimeout(left)

    def _read_stream(self, resp, conn, rid, deadline, status):
        def lines():
            while True:
                self._arm(conn, deadline)
                try:
                    line = resp.readline(1 << 20)
                except http.client.IncompleteRead:
                    return
                if not line:
                    return
                yield line

        for ev in parse_sse(lines()):
            if ev.get("event", "message") != "message" or not ev["data"].strip():
                continue                      # a priming or keep-alive event
            try:
                obj = json.loads(ev["data"])
            except ValueError:
                raise TransportError("sse_parse", status, "an event's data is not JSON: %s"
                                     % ev["data"][:80]) from None
            for m in obj if isinstance(obj, list) else [obj]:
                if isinstance(m, dict) and m.get("id") == rid and ("result" in m or "error" in m):
                    return m
        raise _StreamEnded()

    # ---- errors --------------------------------------------------------------------------------------
    @staticmethod
    def _snippet(body):
        return re.sub(r"\s+", " ", body.decode("utf-8", "replace")).strip()[:200]

    def _raise_http(self, reply):
        st = reply.status
        if st == 401:
            raise TransportError("auth", 401, "401 Unauthorized; WWW-Authenticate: %s"
                                 % (reply.headers.get("www-authenticate") or "(none sent)"))
        code, data = _rpc_error(reply.body)
        if code == -32020:
            raise TransportError("header_mismatch", st, self._snippet(reply.body))
        if code == -32022:
            raise TransportError("unsupported_version", st, "server supports: %s"
                                 % ", ".join(_supported(data) or ["(not listed)"]))
        raise TransportError("http", st, self._snippet(reply.body) or "no body")

    def _result(self, reply):
        if not 200 <= reply.status < 300 or reply.msg is None:
            if 200 <= reply.status < 300:
                raise TransportError("http", reply.status, "no JSON-RPC reply in the response")
            self._raise_http(reply)
        if "error" in reply.msg:
            code, _ = _rpc_error(reply.body)
            if code in (-32020, -32022):
                self._raise_http(reply)
            raise TransportError("http", reply.status, str(reply.msg["error"])[:300])
        res = reply.msg.get("result")
        res = res if isinstance(res, dict) else {}
        if res.get("resultType") == "input_required":
            raise InputRequired(res)
        return res

    # ---- start ---------------------------------------------------------------------------------------
    def start(self):
        """Detect the era and return the discover or initialize result."""
        deadline = time.monotonic() + self.start_timeout
        version = MODERN
        for attempt in (1, 2):
            try:
                reply = self._exchange("server/discover", {}, "", next(self._ids), deadline, "modern", version)
            except TransportError as exc:
                if exc.kind != "sse_parse":
                    raise
                break                         # a stream of garbage is not a modern answer
            if reply.msg is not None and "result" in reply.msg:
                self.era, self.protocol_version = "modern", version
                self.server_info = reply.msg["result"].get("serverInfo")
                return reply.msg["result"]
            if reply.status == 401:
                self._raise_http(reply)
            if detect_era(reply.status, reply.body) == "legacy":
                break
            code, data = _rpc_error(reply.body)
            if code == -32022 and attempt == 1:
                known = [v for v in _supported(data) or [] if v == MODERN]
                if not known:
                    self._raise_http(reply)
                version = known[0]            # retry once, with a version the server listed
                continue
            self._raise_http(reply)
        return self._initialize(deadline)

    def _initialize(self, deadline):
        self.session_id = None
        offered = LEGACY[0]
        reply = self._exchange("initialize", {"protocolVersion": offered, "capabilities": {},
                                              "clientInfo": self.client_info},
                               "", next(self._ids), deadline, "legacy", offered)
        res = self._result(reply)
        version = res.get("protocolVersion")
        if version not in LEGACY:
            raise TransportError("unsupported_version", reply.status,
                                 "server answered initialize with %s; spoken: %s"
                                 % (version, ", ".join(LEGACY)))
        self.era, self.protocol_version = "legacy", version
        self.session_id = reply.headers.get("mcp-session-id") or None
        self.server_info = res.get("serverInfo")
        self.notify("notifications/initialized", None, _deadline=deadline)
        return res

    # ---- requests ------------------------------------------------------------------------------------
    def request(self, method, params=None, timeout=None, name=""):
        """The result of one JSON-RPC request. Raises TransportError (a JSON-RPC error is kind "http") or InputRequired."""
        if self.era is None:
            raise TransportError("connect", 0, "transport not started")
        deadline = time.monotonic() + (self.call_timeout if timeout is None else timeout)
        resends, reinit = 0, False
        while True:
            try:
                reply = self._exchange(method, params, name, next(self._ids), deadline, self.era,
                                       self.protocol_version)
            except _StreamEnded:
                resends += 1
                if resends > RESENDS:
                    raise TransportError("sse_parse", 200, "the stream ended before the reply, %d times"
                                         % resends) from None
                continue
            if reply.status == 404 and self.era == "legacy" and self.session_id:
                if reinit:
                    raise TransportError("session_expired", 404, "the server dropped the session twice")
                reinit = True
                self._initialize(deadline)
                continue
            return self._result(reply)

    def notify(self, method, params=None, _deadline=None):
        deadline = _deadline or time.monotonic() + self.call_timeout
        reply = self._exchange(method, params, "", None, deadline, self.era, self.protocol_version)
        if reply.status == 401:
            self._raise_http(reply)

    def close(self):
        """End a legacy session (DELETE; a 405 is fine). Modern has nothing to end. Never raises."""
        if self.era != "legacy" or not self.session_id:
            return
        hdrs = self._headers("", None, "", "legacy", self.protocol_version)
        self.session_id = None
        try:
            self._send("DELETE", None, hdrs, None, time.monotonic() + CLOSE_TIMEOUT)
        except TransportError:
            pass
