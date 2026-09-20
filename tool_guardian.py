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

try:
    import tg_env  # sibling module: .env autoload + ${VAR}/%VAR% expansion in args
except ImportError:  # degrade gracefully if tg_env.py is not beside this file
    tg_env = None


def _optional(module: str):
    """Sibling modules added in 0.3.0. Each is optional: without it the router
    behaves exactly as 0.2.0 did, so a single-file copy of this script still works."""
    try:
        return __import__(module)
    except ImportError:
        return None


tg_ladder = _optional("tg_ladder")    # deterministic compression of tool results
tg_spill = _optional("tg_spill")      # archive of the full original behind every lossy result
tg_groups = _optional("tg_groups")    # tool groups priced in context tokens

__version__ = "0.3.0"

PROTOCOL = "2024-11-05"
START_TIMEOUT = float(os.environ.get("TOOL_GUARDIAN_START_TIMEOUT", "90"))
CALL_TIMEOUT = float(os.environ.get("TOOL_GUARDIAN_CALL_TIMEOUT", "120"))
CHARS_PER_TOKEN = float(os.environ.get("TOOL_GUARDIAN_CHARS_PER_TOKEN", "3.5"))
LOG_PATH = os.environ.get("TOOL_GUARDIAN_LOG", "")
SKILLS_DIRS = os.environ.get("TOOL_GUARDIAN_SKILLS", "")   # os.pathsep-separated roots
SKILL_FILE = "SKILL.md"
_OFF = ("0", "false", "False", "off", "no")
LADDER_ON = os.environ.get("TOOL_GUARDIAN_LADDER", "1") not in _OFF
# The pre-0.3.0 result shape (the raw MCP envelope as indented JSON, cut at 20000).
RAW_RESULTS = os.environ.get("TOOL_GUARDIAN_RAW_RESULTS", "0") not in _OFF
# One JSON line per router call (and per bypass the DSH plugin reports). "" disables.
CALL_LOG = os.environ.get("TOOL_GUARDIAN_CALL_LOG",
                          str(Path.home() / ".tool-guardian" / "calls.jsonl"))
LEGACY_CAP = 20000


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


def short(desc: str, words: int = 12) -> str:
    """A one-line purpose. The catalogue must stay cheap -- a full description per
    tool would rebuild the very payload this exists to avoid."""
    parts = " ".join((desc or "").split()).split(" ")
    return " ".join(parts[:words]) + ("..." if len(parts) > words else "")


def scan_skills(dirs):
    """Scan skill directories and return skill metadata."""
    skills = []
    if not dirs:
        return skills

    # Handle both string and list
    if isinstance(dirs, str):
        roots = dirs.split(os.pathsep)
    else:
        roots = list(dirs)

    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue

        # Search one level deep
        for item in root_path.iterdir():
            if item.is_dir():
                skill_path = item / SKILL_FILE
                if skill_path.is_file():
                    skills.extend(_parse_skill(skill_path, item.name))

        # Search two levels deep
        for group in root_path.iterdir():
            if group.is_dir():
                for item in group.iterdir():
                    if item.is_dir():
                        skill_path = item / SKILL_FILE
                        if skill_path.is_file():
                            skills.extend(_parse_skill(skill_path, item.name))

    # Sort by name and return
    skills.sort(key=lambda x: x["name"])
    return skills


def _parse_skill(skill_path, dir_name):
    """Parse a SKILL.md file and extract name, description, bytes."""
    try:
        # Read the file content
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()

        skill_size = len(content)

        # Extract frontmatter and body
        lines = content.split('\n')
        name = None
        description = None
        in_frontmatter = False
        body_started = False

        for line in lines:
            stripped = line.strip()

            # Skip empty lines when looking for description
            if not description and body_started:
                if stripped and not stripped.startswith('#'):
                    description = stripped[:200]
                    break

            # Frontmatter parsing
            if stripped == '---':
                if not in_frontmatter:
                    in_frontmatter = True
                elif in_frontmatter and body_started:
                    # End of frontmatter and body
                    break
                continue

            if in_frontmatter and not body_started:
                if ':' in stripped:
                    key, value = stripped.split(':', 1)
                    key = key.strip().lower()
                    value = value.strip()
                    if key == 'name' and not name:
                        name = value
                    elif key == 'description' and not description:
                        description = value
                continue

            if in_frontmatter and stripped and not stripped.startswith('#'):
                body_started = True

        # Use directory name as fallback
        if not name:
            name = dir_name

        # Use directory name as fallback if no description found
        if not description:
            description = "Skill from " + dir_name

        return [{
            "name": name,
            "path": str(skill_path),
            "description": description,
            "bytes": skill_size
        }]

    except Exception:
        # Skip unreadable or malformed skills
        return []


def build_skill_tools(skills):
    """Build the two tools the model sees for skills."""
    if not skills:
        return []

    # Build catalogue
    catalogue_items = []
    for skill in skills:
        catalogue_items.append(f"{skill['name']} ({short(skill['description'])})")
    catalogue = "; ".join(catalogue_items)

    return [
        {"name": "list_skills",
         "description": ("List the skills available. "
                         "THESE SKILLS ARE AVAILABLE AND YOU SHOULD USE THEM RATHER THAN "
                         "GUESSING OR WORKING AROUND THEM: " + catalogue + ". "
                         "If a request concerns any of those, call this FIRST to "
                         "find the right skill, then read_skill."),
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "read_skill",
         "description": ("Get the full text of one skill. "
                         "Call after list_skills if unsure which skill to use."),
         "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"]}},
    ]


def skills_report(skills):
    """Print the saving report for skills."""
    # Report TOKENS, not bytes, so the figure is comparable to the README's
    # tool numbers. CHARS_PER_TOKEN is the file's own estimator. [O5]
    total = int(sum(s["bytes"] for s in skills) / CHARS_PER_TOKEN)
    catalogue = int(len("; ".join("%s (%s)" % (s["name"], short(s["description"]))
                                  for s in skills)) / CHARS_PER_TOKEN)
    saved = total - catalogue
    percentage = (saved / max(total, 1)) * 100

    print("skills: %d found | always-on cost %d tokens | catalogue %d tokens | "
          "saved %d (%.1f%%)" % (len(skills), total, catalogue, saved, percentage))
    return 0


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


def load_options(explicit: str = "") -> dict:
    """The optional `toolGuardian` block beside `mcpServers` in the same config
    file: {"ladder": {...tg_ladder.DEFAULTS overrides}, "groups": {group: [selectors]}}.
    Absent or unreadable -> {} (defaults everywhere)."""
    for path in _config_search_order(explicit):
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                opts = data.get("toolGuardian") or {}
                return opts if isinstance(opts, dict) else {}
        except Exception:  # noqa: BLE001  (load_backends already logged the reason)
            return {}
    return {}


def render_result(res) -> tuple:
    """(text, is_error) from an MCP tools/call result. The text blocks are what a
    model needs; the JSON envelope around them only costs tokens (every newline
    escaped) and hides the payload's real type from the ladder."""
    if not isinstance(res, dict):
        return json.dumps(res, indent=2), False
    parts = []
    for block in res.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, dict):
            parts.append("[%s content]" % block.get("type", "unknown"))
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], indent=2))
    if not parts:
        return json.dumps(res, indent=2), bool(res.get("isError"))
    return "\n".join(parts), bool(res.get("isError"))


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
        args = self.spec.get("args") or []
        if tg_env is not None:
            args = tg_env.expand_args(args)
        cmd = [command, *args]
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


# --------------------------------------------------------------- config ------

class Router:
    def __init__(self, config: str = "", options: dict = None):
        self.config = config
        self.backends = {}
        # The config file's `toolGuardian` block is the base; `options` (the DSH
        # bridge passes settings here) wins key by key.
        self.options = dict(load_options(config))
        self.options.update({k: v for k, v in (options or {}).items() if v not in (None, "", {}, [])})
        self._last_meta = {}
        self.native_groups = False   # True under the DSH plugin, where activate_group exists
        self._spill = None
        self.stats = {"calls": 0, "errors": 0, "shaped": 0, "spilled": 0,
                      "original_chars": 0, "final_chars": 0}

    def start(self, servers: dict = None) -> None:
        """Start every backend AT ONCE. Backend.start() never raises (a failure becomes
        that backend's UNKNOWN status), so the slowest server sets the wait, not the sum:
        seven studio backends took 60 s one after another."""
        specs = servers if servers is not None else load_backends(self.config)
        threads = []
        for name, spec in specs.items():
            b = Backend(name, spec)
            self.backends[name] = b
            t = threading.Thread(target=b.start, name="tg-start-" + name, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

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

    # ---- result shaping (0.3.0) ---------------------------------------------
    def spill_store(self):
        if self._spill is None and tg_spill is not None:
            self._spill = tg_spill.SpillStore(self.options.get("spillDir") or None,
                                              keep=int(self.options.get("spillKeep") or 500))
        return self._spill

    def shape(self, text: str, tool: str = "", is_error: bool = False, archive: bool = True) -> tuple:
        """Run one result down the output ladder. Returns (text, meta). Nothing
        lossy leaves here without its full original archived first: if the archive
        cannot be written, the ORIGINAL goes back (bounded the old way, and saying so)."""
        meta = {"rule": "off", "lossy": False, "original_chars": len(text),
                "final_chars": len(text), "spill_id": ""}
        if not LADDER_ON or tg_ladder is None:
            if len(text) > LEGACY_CAP:
                text = text[:LEGACY_CAP] + "\n[tool-guardian: cut at %d of %d chars; the ladder is off, nothing was archived]" % (LEGACY_CAP, meta["original_chars"])
                meta.update(rule="legacy_cap", lossy=True, final_chars=len(text))
            return text, meta
        out = tg_ladder.compress(text, tool=tool, is_error=is_error,
                                 cfg=self.options.get("ladder") or None)
        meta.update(rule=out["rule"], lossy=bool(out["lossy"]))
        shaped = out["text"]
        if out["lossy"] and not archive:
            # The caller already holds the full original (the DSH harness spilled it and its
            # locator stays on the result), so a second copy here would only be a duplicate.
            meta["spill_id"] = ""
        elif out["lossy"]:
            store = self.spill_store()
            try:
                if store is None:
                    raise OSError("tg_spill.py is not beside tool_guardian.py")
                saved = store.save(text, tool=tool, rule=out["rule"])
                meta["spill_id"] = saved["id"]
                shaped += "\n" + tg_spill.notice(saved["id"], len(text), len(out["text"]))
                self.stats["spilled"] += 1
            except OSError as exc:
                log("spill failed for %s: %s -- returning the original" % (tool, exc))
                shaped = text[:LEGACY_CAP]
                if len(text) > LEGACY_CAP:
                    shaped += "\n[tool-guardian: cut at %d of %d chars; the archive could not be written (%s)]" % (LEGACY_CAP, len(text), exc)
                meta.update(rule="archive_failed")
        if shaped != text:
            self.stats["shaped"] += 1
        meta["final_chars"] = len(shaped)
        return shaped, meta

    def servers_tools(self) -> dict:
        return {n: list(b.tools) for n, b in sorted(self.backends.items()) if b.status == "ok"}

    def log_call(self, entry: dict) -> None:
        """One JSON line per router call / reported bypass. Argument VALUES are never
        written -- only their key names -- because they can carry secrets."""
        self.stats["calls"] += 1 if entry.get("kind") == "router" else 0
        self.stats["errors"] += 1 if entry.get("ok") is False else 0
        self.stats["original_chars"] += int(entry.get("original_chars") or 0)
        self.stats["final_chars"] += int(entry.get("final_chars") or 0)
        if not CALL_LOG:
            return
        try:
            os.makedirs(os.path.dirname(CALL_LOG) or ".", exist_ok=True)
            with open(CALL_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(entry, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))) + "\n")
        except OSError:
            pass

    def handle(self, name: str, args: dict) -> str:
        """Public entry: route, then record. The record is what makes router use
        (and router BYPASS) measurable instead of anecdotal."""
        t0 = time.time()
        raw = dict(args or {}) if isinstance(args, dict) else {}
        entry = {"kind": "router", "tool": name, "server": str(raw.get("server") or ""),
                 "target": str(raw.get("tool") or raw.get("name") or raw.get("id") or ""),
                 "arg_keys": sorted(raw)}
        self._last_meta = {}
        try:
            text = self._handle(name, args)
        except Exception as exc:
            self.log_call(dict(entry, ok=False, ms=int((time.time() - t0) * 1000),
                               error=type(exc).__name__))
            raise
        meta = self._last_meta
        self.log_call(dict(entry, ok=not meta.get("is_error", False),
                           ms=int((time.time() - t0) * 1000),
                           original_chars=meta.get("original_chars", len(text)),
                           final_chars=len(text), rule=meta.get("rule", ""),
                           spill_id=meta.get("spill_id", "")))
        return text

    def _handle(self, name: str, args: dict) -> str:
        # Skills are handled on the RAW args, BEFORE _coerce. _coerce aliases
        # "name" -> "server" and POPS it (it exists so a model saying
        # {"name": "nocodb"} still reaches the right server), which silently ate
        # read_skill's own "name" argument. Caught by the overseer probe, not by
        # any test the lane wrote. [O5] 2026-09-09
        if name in ("list_skills", "read_skill"):
            return self._handle_skill(name, dict(args or {}))
        args = self._coerce(args)
        if name == "list_capabilities":
            return self.catalogue(str(args.get("server") or ""))
        if name == "describe_tool":
            b = self.backends.get(str(args.get("server") or ""))
            if not b:
                return ("no server named %r. Known: %s\n\nNEXT STEP: list_capabilities()"
                        % (args.get("server"), ", ".join(sorted(self.backends))))
            t = b.find(str(args.get("tool") or ""))
            if not t:
                return ("%s has no tool %r. Available: %s\n\nNEXT STEP: "
                        "list_capabilities(server=\"%s\")"
                        % (b.name, args.get("tool"),
                           ", ".join(x.get("name", "?") for x in b.tools), b.name))
            required = (t.get("inputSchema") or {}).get("required") or []
            example = ", ".join('"%s": <%s>' % (k, k) for k in required)
            return (json.dumps(t, indent=2) + "\n\nNEXT STEP: call_tool(server=\"%s\", "
                    "tool=\"%s\", args={%s})" % (b.name, t.get("name"), example))
        if name == "call_tool":
            b = self.backends.get(str(args.get("server") or ""))
            if not b:
                return ("no server named %r. Known: %s\n\nNEXT STEP: list_capabilities()"
                        % (args.get("server"), ", ".join(sorted(self.backends))))
            tool = str(args.get("tool") or "")
            res = b.call(tool, args.get("args") or {})
            if RAW_RESULTS:
                return json.dumps(res, indent=2)[:LEGACY_CAP]
            text, is_error = render_result(res)
            text, meta = self.shape(text, tool=tool, is_error=is_error)
            self._last_meta = dict(meta, is_error=is_error)
            if is_error:
                nxt = ("the tool reported an error. Check its arguments with "
                       "describe_tool(server=\"%s\", tool=\"%s\"), then call_tool again."
                       % (b.name, tool))
            elif meta.get("spill_id"):
                nxt = ("answer the user from this result. It was shortened; if the part you "
                       "need is missing, retrieve_spill(id=\"%s\", grep=\"<regex>\")."
                       % meta["spill_id"])
            else:
                nxt = "you have the result -- answer the user now. Call another tool only if this is not enough."
            return text + "\n\nNEXT STEP: " + nxt
        if name == "list_groups_with_costs":
            if tg_groups is None:
                return "groups are unavailable: tg_groups.py is not beside tool_guardian.py"
            groups = tg_groups.build_groups(self.servers_tools(), self.options.get("groups") or None,
                                            CHARS_PER_TOKEN)
            text = tg_groups.render(groups, est_tokens(ROUTER_TOOLS))
            if not self.native_groups:
                # activate_group exists only under the DSH plugin; do not name a tool this path lacks.
                lines = text.splitlines()
                lines[-1] = ("NEXT STEP: list_capabilities(server=\"<name>\") for the ONE server this "
                             "task needs, then call_tool(server, tool, args).")
                text = "\n".join(lines)
            return text
        if name == "retrieve_spill":
            store = self.spill_store()
            if store is None:
                return "the archive is unavailable: tg_spill.py is not beside tool_guardian.py"
            got = store.get(str(args.get("id") or args.get("spill_id") or ""),
                            start_line=int(args.get("start_line") or 1),
                            max_lines=int(args.get("max_lines") or 400),
                            grep=str(args.get("grep") or ""))
            if not got.get("ok"):
                return "retrieve_spill failed: %s\n\nNEXT STEP: use the id exactly as the notice gave it (sp_ + 12 hex)." % got.get("error")
            tail = ""
            if got.get("truncated"):
                tail = ("\n\nNEXT STEP: more remains -- retrieve_spill(id=\"%s\", start_line=%d) "
                        "or narrow it with grep=\"<regex>\"."
                        % (got["id"], got["start_line"] + got["returned_lines"]))
            return ("[%s: lines %d-%d of %d]\n%s%s"
                    % (got["id"], got["start_line"], got["start_line"] + max(got["returned_lines"] - 1, 0),
                       got["total_lines"], got["text"], tail))
        return "unknown router tool %r\n\nNEXT STEP: list_capabilities()" % name

    def _handle_skill(self, name: str, args: dict) -> str:
        """Skills budget (TJ 2026-09-08): the same catalogue-on-demand shape the
        three router tools use, applied to SKILL.md files. Accepts "skill" or
        "name" for the skill id."""
        args = dict(args or {})
        if not args.get("name") and isinstance(args.get("skill"), str):
            args["name"] = args["skill"]
        if name == "list_skills":
            skills = scan_skills(SKILLS_DIRS)
            if not skills:
                return "no skills configured (set TOOL_GUARDIAN_SKILLS)"
            return "\n".join("%s -- %s" % (sk["name"], short(sk["description"]))
                              for sk in skills)
        if name == "read_skill":
            want = str(args.get("name") or "")
            skills = scan_skills(SKILLS_DIRS)
            names = ", ".join(sk["name"] for sk in skills) or "none"
            # Refuse traversal before touching the filesystem: a name is a NAME,
            # never a path. Everything else is resolved and re-checked below.
            if (not want) or ("/" in want) or ("\\" in want) or (".." in want):
                return "bad skill name %r. Valid: %s" % (want, names)
            match = None
            for sk in skills:
                if sk["name"] == want:
                    match = sk
                    break
            if match is None:
                return "no skill named %r. Valid: %s" % (want, names)
            # scan_skills only ever yields paths under a configured root, and the
            # name was matched against that scan -- so this open is confined by
            # construction, not by trusting the caller's string.
            try:
                with open(match["path"], "r", encoding="utf-8") as fh:
                    return fh.read()
            except OSError as e:
                return "could not read skill %r: %s" % (want, e)
        return "unknown router tool %r" % name


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


def build_extra_tools() -> list:
    """list_groups_with_costs / retrieve_spill, present only when their modules are."""
    extra = []
    if tg_groups is not None:
        extra.append({"name": "list_groups_with_costs",
                      "description": ("List tool GROUPS with the context-token cost of loading each, "
                                      "so you can pick only the group a task needs."),
                      "inputSchema": {"type": "object", "properties": {}}})
    if tg_spill is not None and tg_ladder is not None and LADDER_ON:
        extra.append({"name": "retrieve_spill",
                      "description": ("Read back the FULL original of a result that was shortened. "
                                      "Use the id from the [tool-guardian: ...] notice."),
                      "inputSchema": {"type": "object", "properties": {
                          "id": {"type": "string", "description": "sp_ + 12 hex, from the notice"},
                          "grep": {"type": "string", "description": "regex; returns matching lines"},
                          "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}},
                          "required": ["id"]}})
    return extra


ROUTER_TOOLS = []  # filled at startup by build_router_tools + build_skill_tools


def build_all_tools(backends: dict) -> list:
    """The three router tools, plus the two skill tools when skills are
    configured. With no skills configured the model sees exactly the three it
    saw before this feature existed. [O5] 2026-09-09"""
    return (build_router_tools(backends) + build_extra_tools()
            + build_skill_tools(scan_skills(SKILLS_DIRS)))


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
    ROUTER_TOOLS = build_all_tools(r.backends)
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
    ap.add_argument("--skills-report", dest="skills_report", action="store_true",
                    help="print the token saving for skills configured in "
                         "TOOL_GUARDIAN_SKILLS and exit")
    a = ap.parse_args(argv)
    if a.skills_report:
        skills_report(scan_skills(SKILLS_DIRS))
        return 0
    if a.version:
        print(__version__)
        return 0
    if tg_env is not None:
        # Anchor the .env search on whichever config this run uses, then let
        # tg_env walk up from there to find the project-root .env. A working
        # router always has a config, so this always has an anchor.
        cfg = a.config or os.environ.get("TOOL_GUARDIAN_CONFIG", "")
        if cfg:
            os.environ.setdefault("TOOL_GUARDIAN_CONFIG", cfg)
        tg_env.load_env_file()
    if a.selftest:
        return selftest(a.config)
    r = Router(a.config)
    r.start()
    global ROUTER_TOOLS
    ROUTER_TOOLS = build_all_tools(r.backends)
    log("tool-guardian v%s up with %d backend(s)" % (__version__, len(r.backends)))
    return serve(r)


if __name__ == "__main__":
    raise SystemExit(main())