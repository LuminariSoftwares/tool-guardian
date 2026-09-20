#!/usr/bin/env python3
"""
tg_bridge.py -- private bridge between the dsh-tool-guardian DSH plugin (Node)
and tool_guardian.Router (Python).

This is NOT an MCP server. The DSH plugin registers the router tools natively
and owns the model-facing surface; this process only keeps the real MCP
backends alive and routes calls to them, reusing tool_guardian.py unchanged.
The MCP deployment (`tool-guardian` / `tool_guardian.py`) is untouched and
keeps working for anyone who prefers that path.

PROTOCOL -- one JSON object per line, both directions, UTF-8:
    request   {"id": <int>, "op": "<name>", ...params}
    response  {"id": <int>, "ok": true,  "result": {...}}
              {"id": <int>, "ok": false, "error": "<Type>: <message>"}

    hello      -> versions + interpreter. Starts NOTHING; cheap liveness probe.
    start      {"config": "<path>"?, "mcpServers": {...}?, "options": {...}?}
               -> per-backend status. Inline mcpServers wins over config path.
                  options = {"ladder": {...}, "groups": {...}, "spillDir", "spillKeep"}
    tools      -> the router tool schemas (3 + list_groups_with_costs + retrieve_spill,
                  + 2 when skills are configured)
    call       {"name": "<router tool>", "args": {...}} -> {"text", "isError"}
    ladder     {"text", "tool", "is_error", "archive"?} -> the shaped text for ANY tool's result
               (archive false = the caller already stored the original; shape only)
               (the DSH plugin sends built-in tools' results here): {"text", "rule",
               "lossy", "original_chars", "final_chars", "spill_id"}
    groups     -> {"groups": [...], "router_cost": int, "text": str}
    group_tools {"group": "<name>"} -> {"tools": [{"server","tool","description","inputSchema"}]}
    log        {"entry": {...}} -> appends one line to the call log (bypass reports)
    stats      -> the router's counters since start
    shutdown   -> stops every backend, then exits 0

stdout is the protocol channel and nothing else may write to it: sys.stdout is
re-pointed at stderr on startup so a stray print() anywhere cannot corrupt a
frame (the same rule tool_guardian.log() documents for MCP).

    python tg_bridge.py              serve on stdio
    python tg_bridge.py --selftest   offline checks: no backends, no network
    python tg_bridge.py --selftest-live [--config mcp.json]
                                     LIVE: starts the real backends in the config and proves
                                     catalogue / backends / tool schemas / token saving / ladder

Pure standard library, Python >= 3.9 -- same floor as tool_guardian.py.
MIT licensed.
"""
from __future__ import annotations

import io
import json
import os
import platform
import sys
from pathlib import Path

BRIDGE_VERSION = "0.2.0"

# tool_guardian.py lives one level above modules/, in the repo AND in the
# published npm package (package.json "files"). Resolved from __file__, never
# from the working directory -- DSH may spawn this from anywhere.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import tool_guardian as tg  # noqa: E402  (needs the sys.path entry above)


class Bridge:
    """Owns one Router. Requests are handled one at a time, in arrival order."""

    def __init__(self):
        self.router = None

    # ---- ops ---------------------------------------------------------------
    def op_hello(self, _req: dict) -> dict:
        return {"bridge": BRIDGE_VERSION,
                "tool_guardian": tg.__version__,
                "python": platform.python_version(),
                "executable": sys.executable,
                "package_root": str(PACKAGE_ROOT),
                "pid": os.getpid(),
                "started": self.router is not None}

    def op_start(self, req: dict) -> dict:
        if self.router is not None:
            return self._status()
        config = str(req.get("config") or "")
        inline = req.get("mcpServers") or {}
        if not isinstance(inline, dict):
            raise ValueError("`mcpServers` must be an object")
        if tg.tg_env is not None:
            # Same anchoring main() does: the .env search starts at the config.
            if config:
                os.environ.setdefault("TOOL_GUARDIAN_CONFIG", config)
            tg.tg_env.load_env_file()
        options = req.get("options")
        if options is not None and not isinstance(options, dict):
            raise ValueError("`options` must be an object")
        router = tg.Router(config, options=options)
        router.native_groups = True    # the plugin registers activate_group
        if inline:
            servers = dict(inline)
            servers.pop("tool-guardian", None)  # never front ourselves
            servers.pop("router", None)
            router.start(servers)
        else:
            router.start()
        self.router = router
        tg.ROUTER_TOOLS = tg.build_all_tools(router.backends)
        return self._status()

    def op_tools(self, _req: dict) -> dict:
        backends = self.router.backends if self.router is not None else {}
        return {"tools": tg.build_all_tools(backends)}

    def op_call(self, req: dict) -> dict:
        if self.router is None:
            raise RuntimeError("call before start")
        name = str(req.get("name") or "")
        args = req.get("args") or {}
        try:
            text = self.router.handle(name, args)
            return {"text": text,
                    "isError": bool((getattr(self.router, "_last_meta", None) or {}).get("is_error"))}
        except Exception as exc:  # noqa: BLE001
            # As tool output, exactly like serve(): the model must SEE it.
            return {"text": "ROUTER ERROR: %s: %s" % (type(exc).__name__, exc),
                    "isError": True}

    def _need_router(self):
        if self.router is None:
            raise RuntimeError("call before start")
        return self.router

    def op_ladder(self, req: dict) -> dict:
        router = self._need_router()
        text = req.get("text")
        if not isinstance(text, str):
            raise ValueError("`text` must be a string")
        tool = str(req.get("tool") or "")
        shaped, meta = router.shape(text, tool=tool, is_error=bool(req.get("is_error")),
                                    archive=req.get("archive") is not False)
        router.log_call({"kind": "ladder", "tool": tool, "ok": True, "rule": meta["rule"],
                         "original_chars": meta["original_chars"],
                         "final_chars": meta["final_chars"], "spill_id": meta["spill_id"]})
        return dict(meta, text=shaped)

    def op_groups(self, _req: dict) -> dict:
        router = self._need_router()
        if tg.tg_groups is None:
            raise RuntimeError("tg_groups.py is not beside tool_guardian.py")
        groups = tg.tg_groups.build_groups(router.servers_tools(),
                                           router.options.get("groups") or None,
                                           tg.CHARS_PER_TOKEN)
        cost = tg.est_tokens(tg.ROUTER_TOOLS)
        return {"groups": groups, "router_cost": cost,
                "text": tg.tg_groups.render(groups, cost)}

    def op_group_tools(self, req: dict) -> dict:
        router = self._need_router()
        if tg.tg_groups is None:
            raise RuntimeError("tg_groups.py is not beside tool_guardian.py")
        pairs = tg.tg_groups.resolve(router.servers_tools(),
                                     router.options.get("groups") or None,
                                     str(req.get("group") or ""))
        tools = []
        for server, tool in pairs:
            spec = router.backends[server].find(tool) or {}
            tools.append({"server": server, "tool": tool,
                          "description": spec.get("description") or "",
                          "inputSchema": spec.get("inputSchema") or {"type": "object"}})
        return {"tools": tools}

    def op_log(self, req: dict) -> dict:
        entry = req.get("entry")
        if not isinstance(entry, dict):
            raise ValueError("`entry` must be an object")
        # Works before start too: a bypass can happen while backends are still loading.
        (self.router or tg.Router("", options={})).log_call(dict(entry))
        return {"logged": True}

    def op_stats(self, _req: dict) -> dict:
        return dict(self._need_router().stats)

    def op_shutdown(self, _req: dict) -> dict:
        stopped = self.stop_backends()
        return {"stopped": stopped, "exit": True}

    # ---- helpers -----------------------------------------------------------
    def _status(self) -> dict:
        out = {}
        for name, b in sorted(self.router.backends.items()):
            out[name] = {"status": b.status, "tools": len(b.tools),
                         "error": b.error[:300]}
        return {"backends": out}

    def stop_backends(self) -> int:
        """tool_guardian.Backend has no stop(); end each child here."""
        n = 0
        if self.router is None:
            return n
        for b in self.router.backends.values():
            proc = getattr(b, "proc", None)
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    n += 1
                except OSError:
                    pass
        self.router = None
        return n

    def dispatch(self, req: dict) -> dict:
        rid = req.get("id")
        op = str(req.get("op") or "")
        fn = getattr(self, "op_" + op, None)
        if fn is None:
            return {"id": rid, "ok": False, "error": "UnknownOp: %r" % op}
        try:
            return {"id": rid, "ok": True, "result": fn(req)}
        except Exception as exc:  # noqa: BLE001
            return {"id": rid, "ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc)}


def serve(stdin, proto) -> int:
    bridge = Bridge()
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("frame is not an object")
            except ValueError as exc:
                reply = {"id": None, "ok": False, "error": "BadFrame: %s" % exc}
            else:
                reply = bridge.dispatch(req)
            proto.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proto.flush()
            if reply.get("ok") and (reply.get("result") or {}).get("exit"):
                return 0
    finally:
        bridge.stop_backends()  # stdin closed == the plugin went away
    return 0


# ------------------------------------------------------------- selftest -----

def _check_hello_reports_versions() -> bool:
    r = Bridge().dispatch({"id": 1, "op": "hello"})
    res = r.get("result") or {}
    return (r.get("ok") is True and r.get("id") == 1
            and res.get("tool_guardian") == tg.__version__
            and res.get("bridge") == BRIDGE_VERSION
            and res.get("started") is False)


def _check_unknown_op_is_refused() -> bool:
    r = Bridge().dispatch({"id": 2, "op": "nope"})
    return r.get("ok") is False and "UnknownOp" in str(r.get("error"))


def _check_call_before_start_is_refused() -> bool:
    r = Bridge().dispatch({"id": 3, "op": "call", "name": "list_capabilities"})
    return r.get("ok") is False and "call before start" in str(r.get("error"))


def _check_tools_before_start_has_three_router_tools() -> bool:
    r = Bridge().dispatch({"id": 4, "op": "tools"})
    names = [t.get("name") for t in (r.get("result") or {}).get("tools", [])]
    return names[:3] == ["list_capabilities", "describe_tool", "call_tool"]


def _check_package_root_holds_tool_guardian() -> bool:
    # Default resolution, no overrides: must be the package root, not the CWD.
    return ((PACKAGE_ROOT / "tool_guardian.py").is_file()
            and PACKAGE_ROOT.name != "modules"
            and Path(tg.__file__).resolve().parent == PACKAGE_ROOT)


def _check_serve_frames_roundtrip_and_shutdown_exits() -> bool:
    stdin = io.StringIO('{"id": 7, "op": "hello"}\nnot json\n{"id": 8, "op": "shutdown"}\n'
                        '{"id": 9, "op": "hello"}\n')
    proto = io.StringIO()
    code = serve(stdin, proto)
    frames = [json.loads(x) for x in proto.getvalue().splitlines()]
    return (code == 0 and len(frames) == 3            # frame 9 is never served
            and frames[0]["id"] == 7 and frames[0]["ok"] is True
            and frames[1]["ok"] is False and "BadFrame" in frames[1]["error"]
            and frames[2]["id"] == 8 and frames[2]["result"]["exit"] is True)


def _check_bad_inline_servers_is_refused() -> bool:
    r = Bridge().dispatch({"id": 10, "op": "start", "mcpServers": ["x"]})
    return r.get("ok") is False and "must be an object" in str(r.get("error"))


CHECKS = [
    ("hello_reports_versions", _check_hello_reports_versions),
    ("unknown_op_is_refused", _check_unknown_op_is_refused),
    ("call_before_start_is_refused", _check_call_before_start_is_refused),
    ("tools_before_start_has_three_router_tools",
     _check_tools_before_start_has_three_router_tools),
    ("package_root_holds_tool_guardian", _check_package_root_holds_tool_guardian),
    ("serve_frames_roundtrip_and_shutdown_exits",
     _check_serve_frames_roundtrip_and_shutdown_exits),
    ("bad_inline_servers_is_refused", _check_bad_inline_servers_is_refused),
]


def selftest() -> int:
    passed = 0
    for name, fn in CHECKS:
        try:
            ok = fn() is True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL %s (%s: %s)" % (name, type(exc).__name__, exc))
        else:
            print("  %s %s" % ("ok  " if ok else "FAIL", name))
        if ok:
            passed += 1
    total = len(CHECKS)
    print("tg_bridge selftest: %d checks, %d passed, %d failed"
          % (total, passed, total - passed))
    return 0 if total > 0 and passed == total else 1


def selftest_live(config: str = "") -> int:
    """What the plugin does at load, against the REAL backends, with numbers."""
    import tempfile  # noqa: PLC0415
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        print("  %s %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + detail) if detail else ""))

    bridge = Bridge()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            started = bridge.dispatch({"id": 1, "op": "start", "config": config,
                                       "options": {"spillDir": tmp}})
            backends = (started.get("result") or {}).get("backends") or {}
            up = [n for n, b in backends.items() if b["status"] == "ok"]
            check("config_loads_and_lists_backends", started.get("ok") and len(backends) > 0,
                  "%d configured" % len(backends))
            check("backends_start_and_respond", len(up) > 0,
                  ", ".join("%s=%s(%d)" % (n, b["status"], b["tools"]) for n, b in sorted(backends.items())))
            for name, b in sorted(backends.items()):
                if b["status"] != "ok":
                    print("       %s: %s -- %s" % (name, b["status"], b["error"][:160]))
            tools = (bridge.dispatch({"id": 2, "op": "tools"}).get("result") or {}).get("tools") or []
            names = [t.get("name") for t in tools]
            check("router_tools_return_valid_schemas",
                  names[:3] == ["list_capabilities", "describe_tool", "call_tool"]
                  and all(isinstance(t.get("description"), str) and t["description"]
                          and (t.get("inputSchema") or {}).get("type") == "object" for t in tools),
                  ", ".join(str(n) for n in names))
            cat = bridge.dispatch({"id": 3, "op": "call", "name": "list_capabilities", "args": {}})
            text = (cat.get("result") or {}).get("text") or ""
            check("catalogue_names_every_live_backend", all(("[%s]" % n) in text for n in up))
            groups = bridge.dispatch({"id": 4, "op": "groups"}).get("result") or {}
            full = sum(g["token_cost"] for g in groups.get("groups") or [])
            router_cost = int(groups.get("router_cost") or 0)
            check("token_saving_is_measurable", full > 0 and router_cost > 0,
                  "router ~%d tokens vs ~%d behind it (%.0f%% smaller)"
                  % (router_cost, full, 100.0 * (1 - router_cost / max(full, 1))))
            big = "\n".join("line %05d of a long tool result" % i for i in range(1, 3001))
            shaped = bridge.dispatch({"id": 5, "op": "ladder", "text": big, "tool": "bash"}).get("result") or {}
            check("ladder_saving_is_measurable_and_archived",
                  shaped.get("lossy") is True and str(shaped.get("spill_id", "")).startswith("sp_")
                  and shaped.get("final_chars", 0) < shaped.get("original_chars", 0) / 4,
                  "%s -> %s chars (rule %s)" % (shaped.get("original_chars"), shaped.get("final_chars"), shaped.get("rule")))
            back = bridge.dispatch({"id": 6, "op": "call", "name": "retrieve_spill",
                                    "args": {"id": shaped.get("spill_id", ""), "grep": "line 01500 "}})
            check("lossy_result_is_retrievable", "L1500: line 01500" in ((back.get("result") or {}).get("text") or ""))
        finally:
            bridge.stop_backends()
    total, passed = len(results), sum(results)
    print("tg_bridge selftest-live: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if total > 0 and passed == total else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest-live" in argv:
        config = argv[argv.index("--config") + 1] if "--config" in argv and argv.index("--config") + 1 < len(argv) else ""
        return selftest_live(config)
    if "--selftest" in argv:
        return selftest()
    if "--version" in argv:
        print(BRIDGE_VERSION)
        return 0
    # Windows defaults stdio to a legacy code page; the protocol is UTF-8.
    proto = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr  # nothing but `proto` may reach the real stdout
    return serve(stdin, proto)


if __name__ == "__main__":
    raise SystemExit(main())
