"""
tool_guardian.py
================
An MCP server that fronts your other MCP servers and exposes THREE generic tools
instead of dozens of specific ones, discovering the rest on demand:

    list_capabilities(server?)      one line per tool -- names and purpose
    describe_tool(server, tool)     the full argument schema for ONE tool
    call_tool(server, tool, args)   invoke it, return the result

    tool-guardian                      run as an MCP server (stdio)
    tool-guardian --selftest           start the backends, print the token saving
    tool-guardian --config path.json   use a specific mcpServers config

WHY THIS EXISTS
    MCP tool definitions are re-sent on EVERY request, whether the model touches
    them or not. A handful of servers routinely comes to tens of thousands of
    tokens -- most of a small local model's context window -- before the first
    user message. Loading fewer servers trades capability for room. This trades
    neither: the model sees ~300 tokens of router tools and the full catalogue
    only when it asks. Same progressive-disclosure idea as a search index --
    cheap catalogue first, detail on demand.

    Companion to Context Guardian (https://pypi.org/project/context-guardian/):
    Context Guardian compacts the CONVERSATION before the window fills; Tool
    Guardian keeps the TOOLS from filling it in the first place. Two halves of
    the same problem.

FAILURE IS LOUD, ON PURPOSE
    A router is a single point of failure: without one, a broken server costs
    you that server; behind one it could cost you all of them. So an unreachable
    backend is reported as UNKNOWN with its real error, NEVER as an empty tool
    list. A model that asks for a server and gets `[]` concludes the capability
    does not exist and quietly works around it -- the exact failure this avoids.

CONFIG
    Standard MCP shape -- the same `mcpServers` block Claude Desktop / Claude
    Code / most MCP clients use:

        {"mcpServers": {
            "files":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]},
            "git":    {"command": "uvx", "args": ["mcp-server-git"], "description": "git status/diff/commit"}
        }}

    An optional per-server "description" enriches the catalogue the model sees;
    without it, the hint is derived from the server's own tool names at startup.
    Searched in order: --config PATH, $TOOL_GUARDIAN_CONFIG, ./mcp.json,
    ./.mcp.json, ~/.tool-guardian/mcp.json.

    stdio servers only for now. An HTTP/SSE server (a "url" entry) is reported
    UNSUPPORTED -- load it directly rather than through here.

MIT licensed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

__version__ = "0.1.0"

PROTOCOL = "2024-11-05"
START_TIMEOUT = float(os.environ.get("TOOL_GUARDIAN_START_TIMEOUT", "90"))
CALL_TIMEOUT = float(os.environ.get("TOOL_GUARDIAN_CALL_TIMEOUT", "120"))
CHARS_PER_TOKEN = float(os.environ.get("TOOL_GUARDIAN_CHARS_PER_TOKEN", "3.5"))
LOG_PATH = os.environ.get("TOOL_GUARDIAN_LOG", "")


def log(msg: str) -> None:
    """stderr and (optionally) a file. NEVER stdout -- stdout is the MCP channel
    and one stray line there corrupts the protocol for the whole session."""
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, file=sys.stderr, flush=True)
    if LOG_PATH:
        try:
            os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def est_tokens(obj) -> int:
    """Rough token estimate from serialized length (~3.5 chars/token). Not a real
    tokenizer -- a safety-margin figure for the saving report, deliberately in
    the conservative (slightly high) direction, same as Context Guardian."""
    try:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(obj)
    return int(len(text) / CHARS_PER_TOKEN)


# --------------------------------------------------------------- config ------
def _config_search_order(explicit: str = "") -> list:
    order = []
    if explicit:
        order.append(Path(explicit))
    env = os.environ.get("TOOL_GUARDIAN_CONFIG", "").strip()
    if env:
        order.append(Path(env))
    order += [Path("mcp.json"), Path(".mcp.json"),
              Path.home() / ".tool-guardian" / "mcp.json"]
    return order


def load_backends(explicit: str = "") -> dict:
    """Return {name: spec} from the first config file found. spec is the standard
    MCP server object: command, args, env, optional description/url."""
    for path in _config_search_order(explicit):
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers") or data.get("servers") or {}
                if not isinstance(servers, dict):
                    raise ValueError("`mcpServers` is not an object")
                log("config: %d server(s) from %s" % (len(servers), path))
                servers.pop("tool-guardian", None)  # never front ourselves
                servers.pop("router", None)
                return servers
        except Exception as exc:  # noqa: BLE001
            log("config %s unreadable: %s" % (path, exc))
    log("no config found (looked for mcp.json / .mcp.json / "
        "$TOOL_GUARDIAN_CONFIG / --config). Running with no backends.")
    return {}


# -------------------------------------------------------------- backend ------
class Backend:
    """One real MCP server, kept alive so tools/call does not pay a cold start."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec or {}
        self.proc = None
        self.tools = []
        self.status = "not started"
        self.error = ""
        self._id = 100
        self._lock = threading.Lock()

    def _send(self, obj) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _await(self, want_id: int, timeout: float):
        """Read until the reply with this id. Servers emit notifications and log
        lines on stdout, so anything else is skipped, not treated as the answer."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:300])
                return msg.get("result", {})
        raise TimeoutError("no reply to id %d within %ss" % (want_id, timeout))

    def _next(self) -> int:
        self._id += 1
        return self._id

    def start(self) -> None:
        if self.spec.get("url"):
            self.status = "UNSUPPORTED"
            self.error = ("HTTP/SSE backend -- this router speaks stdio only so "
                          "far. Load it directly rather than through here.")
            return
        command = self.spec.get("command")
        if not command:
            self.status, self.error = "UNKNOWN", "no `command` in config"
            return
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (self.spec.get("env") or {}).items()})
        cmd = [command, *(self.spec.get("args") or [])]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, text=True,
                encoding="utf-8", errors="replace", bufsize=1)
        except Exception as exc:  # noqa: BLE001
            self.status, self.error = "UNKNOWN", "could not start: %s" % exc
            return
        try:
            i = self._next()
            self._send({"jsonrpc": "2.0", "id": i, "method": "initialize",
                        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "tool-guardian",
                                                  "version": __version__}}})
            self._await(i, START_TIMEOUT)
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            i = self._next()
            self._send({"jsonrpc": "2.0", "id": i, "method": "tools/list", "params": {}})
            res = self._await(i, START_TIMEOUT)
            self.tools = res.get("tools") or []
            self.status = "ok"
            log("backend %s: %d tools" % (self.name, len(self.tools)))
        except Exception as exc:  # noqa: BLE001
            self.status, self.error = "UNKNOWN", "%s: %s" % (type(exc).__name__, exc)
            log("backend %s: %s -- %s" % (self.name, self.status, self.error))

    def hint(self) -> str:
        """A one-line purpose for the catalogue: the config `description` if
        given, else derived from the server's own tool names."""
        desc = self.spec.get("description")
        if desc:
            return short(desc)
        if self.tools:
            names = ", ".join(t.get("name", "?") for t in self.tools[:6])
            return names + ("..." if len(self.tools) > 6 else "")
        return "see list_capabilities"

    def find(self, tool: str):
        for t in self.tools:
            if t.get("name") == tool:
                return t
        return None

    def call(self, tool: str, args: dict):
        if self.status != "ok":
            raise RuntimeError(
                "server %r is %s: %s. That is UNKNOWN, not 'the tool does not "
                "exist' -- do not work around it, report it."
                % (self.name, self.status, self.error))
        if not self.find(tool):
            raise RuntimeError(
                "server %r has no tool %r. Available: %s"
                % (self.name, tool, ", ".join(t.get("name", "?") for t in self.tools)))
        with self._lock:
            i = self._next()
            self._send({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                        "params": {"name": tool, "arguments": args or {}}})
            return self._await(i, CALL_TIMEOUT)


def short(desc: str, words: int = 12) -> str:
    """A one-line purpose. The catalogue must stay cheap -- a full description per
    tool would rebuild the very payload this exists to avoid."""
    parts = " ".join((desc or "").split()).split(" ")
    return " ".join(parts[:words]) + ("..." if len(parts) > words else "")


def build_router_tools(backends: dict) -> list:
    """The 3 tools the model actually sees. The catalogue of server NAMES goes in
    the description on purpose: three unnamed generic tools give the model no
    reason to believe any capability exists, so it improvises instead of calling
    them. Naming the servers costs ~a few tokens and is the difference between a
    catalogue the model opens and one it ignores."""
    live = [(n, b) for n, b in sorted(backends.items()) if b.status == "ok"]
    catalogue = "; ".join("%s (%s)" % (n, b.hint()) for n, b in live) or "none reachable"
    return [
        {"name": "list_capabilities",
         "description": ("List the tools on a connected MCP server. THESE SERVERS "
                         "ARE AVAILABLE AND YOU SHOULD USE THEM RATHER THAN "
                         "GUESSING OR WORKING AROUND THEM: " + catalogue + ". "
                         "If a request concerns any of those, call this FIRST to "
                         "find the right tool, then call_tool."),
         "inputSchema": {"type": "object", "properties": {
             "server": {"type": "string", "description": "server name, or omit for all"}}}},
        {"name": "describe_tool",
         "description": ("Get the full argument schema for one tool. Call after "
                         "list_capabilities and before call_tool if unsure of args."),
         "inputSchema": {"type": "object", "properties": {
             "server": {"type": "string"}, "tool": {"type": "string"}},
             "required": ["server", "tool"]}},
        {"name": "call_tool",
         "description": ("Invoke a tool on a server. THIS IS THE STEP THAT ANSWERS "
                         "THE USER -- list_capabilities only finds the name. `args` "
                         "may be an object or a JSON string; omit it for tools that "
                         "take no arguments."),
         "inputSchema": {"type": "object", "properties": {
             "server": {"type": "string"}, "tool": {"type": "string"},
             "args": {"description": "the tool's arguments -- object or JSON string"}},
             "required": ["server", "tool"]}},
    ]


ROUTER_TOOLS = []  # filled at startup by build_router_tools


class Router:
    def __init__(self, config: str = ""):
        self.config = config
        self.backends = {}

    def start(self) -> None:
        for name, spec in load_backends(self.config).items():
            b = Backend(name, spec)
            b.start()
            self.backends[name] = b

    def catalogue(self, server: str = "") -> str:
        if server:
            b = self.backends.get(server)
            if not b:
                return ("no server named %r. Known: %s"
                        % (server, ", ".join(sorted(self.backends))))
            if b.status != "ok":
                return ("%s: %s -- %s\nThis is UNKNOWN, not an empty tool list."
                        % (server, b.status, b.error))
            listing = "\n".join("%s.%s: %s" % (server, t.get("name"),
                                               short(t.get("description")))
                                for t in b.tools)
            # The next step goes in the RESULT, not only the tool description: a
            # description is read once, before the model has the catalogue; a
            # result is read at the moment the model decides what to do next.
            return (listing + "\n\nNEXT STEP: you have not answered the user yet. "
                    "Pick the tool above that does the job and call it now:\n"
                    "  call_tool(server=\"%s\", tool=\"<name>\", args={...})" % server)
        out = []
        for name, b in sorted(self.backends.items()):
            if b.status != "ok":
                out.append("[%s] %s: %s" % (name, b.status, b.error[:80]))
                continue
            out.append("[%s] %d tools: %s" % (name, len(b.tools),
                       ", ".join(t.get("name", "?") for t in b.tools)))
        if out:
            out.append("\nNEXT STEP: call list_capabilities(server=\"<name>\") for "
                       "one server's full tool list, then call_tool to invoke.")
        return "\n".join(out) or "no backends configured"

    @staticmethod
    def _coerce(args: dict) -> dict:
        """Accept what a model actually sends, not only what the schema says.
        Models commonly send args as a JSON STRING, or name the server `query`/
        `name`. A strict schema is right for a machine caller and wrong for a
        model one -- it loses correct answers on JSON shape. Parse and alias;
        refuse only what is genuinely ambiguous."""
        a = dict(args or {})
        for alias in ("query", "name", "server_name"):
            if not a.get("server") and isinstance(a.get(alias), str):
                a["server"] = a.pop(alias)
        for alias in ("tool_name", "toolName"):
            if not a.get("tool") and a.get(alias):
                a["tool"] = a.pop(alias)
        raw = a.get("args")
        if isinstance(raw, str):
            try:
                a["args"] = json.loads(raw) if raw.strip() else {}
            except ValueError:
                a["args"] = {}
        elif raw is None:
            a["args"] = {}
        return a

    def handle(self, name: str, args: dict) -> str:
        args = self._coerce(args)
        if name == "list_capabilities":
            return self.catalogue(str(args.get("server") or ""))
        if name == "describe_tool":
            b = self.backends.get(str(args.get("server") or ""))
            if not b:
                return ("no server named %r. Known: %s"
                        % (args.get("server"), ", ".join(sorted(self.backends))))
            t = b.find(str(args.get("tool") or ""))
            if not t:
                return ("%s has no tool %r. Available: %s"
                        % (b.name, args.get("tool"),
                           ", ".join(x.get("name", "?") for x in b.tools)))
            return json.dumps(t, indent=2)
        if name == "call_tool":
            b = self.backends.get(str(args.get("server") or ""))
            if not b:
                return ("no server named %r. Known: %s"
                        % (args.get("server"), ", ".join(sorted(self.backends))))
            res = b.call(str(args.get("tool") or ""), args.get("args") or {})
            return json.dumps(res, indent=2)[:20000]
        return "unknown router tool %r" % name


def serve(router: Router) -> int:
    """Speak MCP on stdio. stdout is the protocol -- see log()."""
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                     "serverInfo": {"name": "tool-guardian", "version": __version__}}
        elif method == "tools/list":
            reply = {"tools": ROUTER_TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text = router.handle(p.get("name", ""), p.get("arguments") or {})
                reply = {"content": [{"type": "text", "text": text}]}
            except Exception as exc:  # noqa: BLE001
                # As tool output, not a protocol error: the model must SEE the
                # failure and say so rather than silently retry.
                reply = {"content": [{"type": "text",
                                      "text": "ROUTER ERROR: %s: %s"
                                              % (type(exc).__name__, exc)}],
                         "isError": True}
        elif mid is None:
            continue  # a notification
        else:
            reply = {}
        if mid is not None:
            out.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": reply}) + "\n")
            out.flush()
    return 0


def selftest(config: str = "") -> int:
    r = Router(config)
    r.start()
    global ROUTER_TOOLS
    ROUTER_TOOLS = build_router_tools(r.backends)
    print(r.catalogue())
    ok = [b for b in r.backends.values() if b.status == "ok"]
    bad = [b for b in r.backends.values() if b.status != "ok"]
    router_cost = est_tokens(ROUTER_TOOLS)
    full_cost = sum(est_tokens(b.tools) for b in ok)
    print("\nrouter tools cost ~%d tokens vs ~%d for the full set behind them"
          % (router_cost, full_cost))
    if full_cost > router_cost:
        print("-> ~%d tokens freed on every request (%.0f%% smaller)"
              % (full_cost - router_cost, 100.0 * (1 - router_cost / max(full_cost, 1))))
    else:
        print("-> at this size the router's own tools cost about as much as the "
              "servers behind it. The win grows with more / larger servers -- it "
              "pays off exactly when tool bloat is actually a problem.")
    print("\n%d backend(s) up, %d tools reachable."
          % (len(ok), sum(len(b.tools) for b in ok)))
    for b in bad:
        print("  %s: %s -- %s" % (b.name, b.status, b.error[:100]))
    print("\nNOTE: this measures the token SAVING and that backends start. It does"
          "\nNOT prove your model will call list_capabilities/call_tool -- smaller"
          "\nlocal models often bypass the router and use built-in tools instead."
          "\nVerify discovery->call with your real model first. See the README's"
          "\n'Model requirement' section.")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tool Guardian -- an MCP router that "
                                 "keeps tool definitions from filling the context window")
    ap.add_argument("--config", default="", help="path to an mcpServers JSON config")
    ap.add_argument("--selftest", action="store_true",
                    help="start backends, print the token saving, exit")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print(__version__)
        return 0
    if a.selftest:
        return selftest(a.config)
    r = Router(a.config)
    r.start()
    global ROUTER_TOOLS
    ROUTER_TOOLS = build_router_tools(r.backends)
    log("tool-guardian v%s up with %d backend(s)" % (__version__, len(r.backends)))
    return serve(r)


if __name__ == "__main__":
    raise SystemExit(main())
