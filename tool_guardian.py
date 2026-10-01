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
    tool-guardian --bypass-summary     router use vs shell bypasses, from the call log
    tool-guardian --hook-pretooluse    Claude Code PreToolUse hook: logs (or denies) shell
                                       calls that do a router tool's job

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
import collections
import hashlib
import json
import os
import re
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
tg_state = _optional("tg_state")      # session ids, state.json, call-log summary, Claude Code hook
tg_update = _optional("tg_update")    # the one "update available" line

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


def compact_args(schema) -> str:
    """One-line argument summary, `name:type` with `?` for optional (2026-10-01, bench1001: discovery took 4.8 model
    requests per task because search -> describe -> call needed a describe round trip just to learn the arguments)."""
    props = (schema or {}).get("properties") or {}
    req = set((schema or {}).get("required") or [])
    if not props:
        return "(no arguments)"
    parts = []
    for name, spec in props.items():
        spec = spec or {}
        typ = spec.get("type", "any")
        if isinstance(typ, list):
            typ = "|".join(str(t) for t in typ)
        item = "%s%s:%s" % ("" if name in req else "?", name, typ)
        if spec.get("enum"):
            item += "{" + "|".join(str(v) for v in spec["enum"][:6]) + "}"
        parts.append(item)
    return ", ".join(parts)


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
    # tool numbers. CHARS_PER_TOKEN is the file's own estimator.
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


# ------------------------------------------------------ argument checking ------

def _coerce_arg(value, want):
    """One SAFE coercion of `value` towards the declared type, or the value back
    unchanged. Safe means the conversion cannot change what the caller meant: JSON
    draws no distinction between 2 and "2" the way a model's output does, so the
    quote is punctuation, not meaning. Anything less certain (a bare word for an
    integer) is left alone and reported as a problem instead."""
    if want == "integer":
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value.strip())
    elif want == "number":
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return value          # not a number after all -- let it be reported
    elif want == "boolean":
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    elif want == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    elif want == "array" and not isinstance(value, list):
        return [value]
    return value


def _arg_is_type(value, want) -> bool:
    """Does the (possibly coerced) value satisfy the declared JSON Schema type?
    A bool is never an integer: Python says otherwise, JSON does not, and the model
    that sent `true` for a count did not send 1. An unknown type name passes."""
    if want == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "string":
        return isinstance(value, str)
    if want == "boolean":
        return isinstance(value, bool)
    if want == "array":
        return isinstance(value, list)
    if want == "object":
        return isinstance(value, dict)
    if want == "null":
        return value is None
    return True


def validate_args(schema, args) -> tuple:
    """Check one tools/call argument object against a tool's declared inputSchema,
    BEFORE it costs a backend round trip. Returns (ok, fixed, problems): `fixed` is
    what should actually be sent -- the caller's arguments with only the safe
    coercions applied -- and `problems` is the human-readable list the model gets
    back instead of a server-side error it has to guess about.

    Pure: no I/O, no globals, no clock. Same input, same output, so it can be read
    as the definition of the contract rather than as behaviour."""
    if not isinstance(schema, dict) or not (schema.get("properties") or schema.get("required")):
        # Nothing declared, so nothing to check -- which is also why a caller that
        # sent no args at all (None) is fine here: a tool that declares nothing
        # takes nothing, and {} is the honest reading of an absent `args`.
        return True, (dict(args) if isinstance(args, dict) else {}), []
    if not isinstance(args, dict):
        return False, {}, ["arguments must be a JSON object"]
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
    required = schema.get("required")
    if not isinstance(required, (list, tuple)):
        required = []
    problems = []
    for name in required:
        if name not in args:
            spec = props.get(name)
            want = spec.get("type") if isinstance(spec, dict) else None
            problems.append("missing required argument '%s' (%s)" % (name, want or "any"))
    fixed = {}
    for key, value in args.items():
        spec = props.get(key)
        if isinstance(spec, dict) and isinstance(spec.get("type"), str):
            value = _coerce_arg(value, spec["type"])
            if not _arg_is_type(value, spec["type"]):
                problems.append("argument '%s' should be %s, got %s"
                                % (key, spec["type"], type(value).__name__))
        enum = spec.get("enum") if isinstance(spec, dict) else None
        if isinstance(enum, (list, tuple)) and value not in enum:
            problems.append("argument '%s' must be one of: %s"
                            % (key, ", ".join(map(str, enum))))
        if key not in props and schema.get("additionalProperties") is False:
            problems.append("unknown argument '%s'; allowed: %s"
                            % (key, ", ".join(sorted(props))))
        fixed[key] = value
    # Nested objects are not walked: a schema is a floor, not a compiler, and a
    # half-validated tree is better than none for the tool that wants it whole.
    return not problems, fixed, problems


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
                      "original_chars": 0, "final_chars": 0,
                      "schema_rejects": 0, "schema_coercions": 0,
                      "loop_warnings": 0, "loop_blocks": 0,
                      "recalls": {}}
        # Spill id -> the tool whose result it holds, and the ids already counted
        # as recalled. A shortened result the model keeps reading back is a result being
        # shortened too hard, and that is only visible if the two ends are joined up.
        self._spill_origin = {}
        self._recalled = set()
        # (key, result_hash) for the last 20 call_tool calls. The loop guard's whole
        # memory: a model retrying an identical call is invisible per-call, so it
        # is only visible by remembering what the last few calls returned.
        self._recent = collections.deque(maxlen=20)
        # Every call-log row carries this, so "the last session" is a question the log can answer.
        self.session_id = tg_state.new_session_id() if tg_state is not None else ""

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
        self.remember_catalogue()

    def remember_catalogue(self) -> None:
        """Tool names per live server into state.json: the Claude Code hook matches shell
        commands against them without starting a single backend. Best effort."""
        if tg_state is None:
            return
        path = tg_state.default_state_path()
        if not path:
            return
        try:
            tg_state.record_catalogue(path, {n: [t.get("name", "") for t in b.tools]
                                             for n, b in self.backends.items() if b.status == "ok"})
        except Exception as exc:  # noqa: BLE001
            log("state.json not updated: %s" % exc)

    def log_session_start(self, harness: str, client: str = "") -> None:
        """One `kind: session` row. A session with this row and no router row is one
        where the model never used the router -- the one bypass signal a plain MCP
        server can see on its own."""
        entry = {"kind": "session", "event": "start", "harness": harness, "ok": True}
        if client:
            entry["client"] = client[:80]
        self.log_call(entry)

    def catalogue(self, server: str = "") -> str:
        if server:
            b = self.backends.get(server)
            if not b:
                return ("no server named %r. Known: %s"
                        % (server, ", ".join(sorted(self.backends))))
            if b.status != "ok":
                return ("%s: %s -- %s\nThis is UNKNOWN, not an empty tool list."
                        % (server, b.status, b.error))
            listing = "\n".join("%s.%s: %s\n    args: %s" % (server, t.get("name"),
                                                             short(t.get("description")),
                                                             compact_args(t.get("inputSchema")))
                                for t in b.tools)
            # The next step goes in the RESULT, not only the tool description: a
            # description is read once, before the model has the catalogue; a
            # result is read at the moment the model decides what to do next.
            # 2026-10-01: no call-shaped example in results -- small models copied it as TEXT instead of calling.
            return (listing + "\n\nNEXT STEP: you have not answered the user yet. Pick the tool above that does "
                    "the job and use call_tool with server %s, that tool's name, and the args listed under it." % server)
        out = []
        for name, b in sorted(self.backends.items()):
            if b.status != "ok":
                out.append("[%s] %s: %s" % (name, b.status, b.error[:80]))
                continue
            out.append("[%s] %d tools: %s" % (name, len(b.tools),
                       ", ".join(t.get("name", "?") for t in b.tools)))
        if out:
            out.append("\nNEXT STEP: use search_capabilities with a keyword (or list_capabilities with one "
                       "server name) to see a tool's args, then call_tool.")
        return "\n".join(out) or "no backends configured"

    def search(self, query: str, server: str = "", limit=None) -> str:
        """Find a tool by keyword, one line per hit, in the catalogue's own format.
        The full listing of a big server is expensive in both directions -- the
        model pays for it to read and the server pays to produce it -- while the
        model usually wants one of the forty lines. This returns the ranked few.

        Scoring is deliberately simple and explainable (a name hit beats a
        description hit; a server-name hit is a strong signal the caller is asking
        about that server): the model is told WHICH line won and why the others did
        not, not handed a number it cannot act on."""
        if not (query or "").strip():
            return self.catalogue(server)
        if server and (server not in self.backends
                       or self.backends[server].status != "ok"):
            # An unknown or dead server gets catalogue()'s own loud wording. A silent
            # "no matches" here would read as "this server has no such tool", which
            # is the exact misreading this file refuses to allow.
            return self.catalogue(server)
        tokens = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 1]
        hits = []
        for name, b in sorted(self.backends.items()):
            if server and name != server:
                continue
            if b.status != "ok":
                continue
            for t in b.tools:
                tname = str(t.get("name") or "")
                low_desc = short(t.get("description")).lower()
                score = 0
                for tok in tokens:
                    if tok in tname.lower():
                        score += 3
                    if tok == name.lower():
                        score += 2
                    if tok in low_desc:
                        score += 1
                if score:
                    hits.append((score, "%s.%s" % (name, tname),
                                 "%s.%s: %s" % (name, tname, short(t.get("description")))))
        if not hits:
            return ("no tools match %r. NEXT STEP: try another word, or use list_capabilities to see "
                    "every server." % query)
        hits.sort(key=lambda h: (-h[0], h[1]))
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 8
        top = hits[:max(1, min(25, n))]
        out = ["%d match(es) for %r:" % (len(hits), query)]
        for i, h in enumerate(top):
            out.append(h[2])
            if i < 3:  # bounded: the args of the likeliest hits, so the call needs no describe round trip
                srv, tname = h[1].split(".", 1)
                found = self.backends[srv].find(tname)
                if found is not None:
                    out.append("    args: " + compact_args(found.get("inputSchema")))
        out.append("")
        out.append("NEXT STEP: use call_tool with the server and tool of the line that fits (server.tool) "
                   "and the args shown under it; describe_tool only if those args are unclear.")
        return "\n".join(out)

    @staticmethod
    def _coerce(args: dict, name: str = "") -> dict:
        """Accept what a model actually sends, not only what the schema says.
        Models commonly send args as a JSON STRING, or name the server `query`/
        `name`. A strict schema is right for a machine caller and wrong for a
        model one -- it loses correct answers on JSON shape. Parse and alias;
        refuse only what is genuinely ambiguous."""
        a = dict(args or {})
        # `query` is an alias for `server` everywhere EXCEPT on search_capabilities,
        # where it is the search text itself: aliasing there would consume the very
        # words being searched for and leave an empty query. [T1]
        aliases = ("name", "server_name") if name == "search_capabilities" \
            else ("query", "name", "server_name")
        for alias in aliases:
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
                try:
                    key = tool or "unknown"
                    self.stats["recalls"].setdefault(key, {"spilled": 0, "recalled": 0})["spilled"] += 1
                    self._spill_origin[saved["id"]] = key
                except Exception:  # a counter must never cost the caller its result
                    pass
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
        row = dict(entry, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
        if self.session_id and not row.get("session"):
            row["session"] = self.session_id
        try:
            os.makedirs(os.path.dirname(CALL_LOG) or ".", exist_ok=True)
            with open(CALL_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
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
        # read_skill's own "name" argument. Caught by a contract probe, not by
        # the module's own tests.
        if name in ("list_skills", "read_skill"):
            return self._handle_skill(name, dict(args or {}))
        args = self._coerce(args, name)
        if name == "list_capabilities":
            return self.catalogue(str(args.get("server") or ""))
        if name == "search_capabilities":
            return self.search(str(args.get("query") or ""), str(args.get("server") or ""),
                               args.get("limit"))
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
            return (json.dumps(t, indent=2) + "\n\nNEXT STEP: use call_tool with server %s, tool %s, "
                    "args: %s." % (b.name, t.get("name"), compact_args(t.get("inputSchema"))))
        if name == "call_tool":
            b = self.backends.get(str(args.get("server") or ""))
            if not b:
                return ("no server named %r. Known: %s\n\nNEXT STEP: list_capabilities()"
                        % (args.get("server"), ", ".join(sorted(self.backends))))
            tool = str(args.get("tool") or "")
            sent = args.get("args") or {}
            # ---- T2: check the arguments against the tool's own schema first. A
            # call that cannot possibly work should cost a string comparison here,
            # not a backend round trip and a server-side error the model must decode.
            if os.environ.get("TG_VALIDATE_ARGS", "1") not in _OFF:
                found = b.find(tool)
                if found is not None:
                    try:
                        ok, fixed, problems = validate_args(found.get("inputSchema") or {}, sent)
                    except Exception as exc:  # noqa: BLE001
                        # Fail OPEN. A checker that crashes must not become a tool
                        # that cannot be called at all.
                        log("argument check failed for %s.%s: %s -- sending unchanged"
                            % (b.name, tool, exc))
                        ok, fixed, problems = True, sent, []
                    if not ok:
                        self.stats["schema_rejects"] += 1
                        self._last_meta = {"is_error": True, "rule": "schema_reject"}
                        # 2026-10-01: list the arguments, so a retry needs no describe_tool round trip.
                        return ("call_tool was not sent: the arguments do not match %s.%s's schema:\n- "
                                % (b.name, tool)
                                + "\n- ".join(problems)
                                + "\n\nNEXT STEP: retry call_tool for %s.%s with args: %s."
                                % (b.name, tool, compact_args(found.get("inputSchema"))))
                    if fixed != sent:
                        self.stats["schema_coercions"] += 1
                    sent = fixed
            # ---- T3: the same call, answered the same way, again and again. Nothing
            # downstream changes between attempt 3 and attempt 40, so the model is
            # told so rather than left paying for a result it already has.
            guard = os.environ.get("TG_LOOP_GUARD", "1") not in _OFF
            loop_key = (b.name, tool, json.dumps(sent, sort_keys=True, default=str))
            if guard:
                prior = [h for k, h in self._recent if k == loop_key]
                if len(prior) >= 4 and len(set(prior[-4:])) == 1:
                    self.stats["loop_blocks"] += 1
                    self._last_meta = {"is_error": True, "rule": "loop_block"}
                    return ("[tool-guardian: this exact call was not run again -- it has "
                            "returned the same result %d times. Use the result you already "
                            "have, change the arguments, or tell the user what is blocking "
                            "you.]" % len(prior[-4:]))
            res = b.call(tool, sent)
            if RAW_RESULTS:
                return json.dumps(res, indent=2)[:LEGACY_CAP]
            text, is_error = render_result(res)
            loop_prefix = ""
            if guard:
                # sha1 as a FINGERPRINT, not as a security primitive: it only has to
                # tell "this result is byte-identical to the last one" cheaply, and
                # the result is never hashed for anything that needs to be unforgeable.
                digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()  # nosec B324
                self._recent.append((loop_key, digest))
                same = [h for k, h in self._recent if k == loop_key]
                if len(same) >= 3 and len(set(same[-3:])) == 1:
                    self.stats["loop_warnings"] += 1
                    run = 1
                    while run < len(same) and same[-1 - run] == same[-1]:
                        run += 1
                    loop_prefix = ("[tool-guardian: this exact call has now run %d times "
                                   "with the same result. Repeating it will not change the "
                                   "answer -- use what you have, change the arguments, or "
                                   "tell the user what is blocking you.]\n" % run)
            text, meta = self.shape(text, tool=tool, is_error=is_error)
            self._last_meta = dict(meta, is_error=is_error)
            if is_error:
                nxt = ("the tool reported an error. Check the arguments of %s.%s (describe_tool), then "
                       "use call_tool again." % (b.name, tool))
            elif meta.get("spill_id"):
                nxt = ("answer the user from this result. It was shortened; if the part you "
                       "need is missing, use retrieve_spill with id %s and a grep regex."
                       % meta["spill_id"])
            else:
                nxt = "answer the user from this result."
            return loop_prefix + text + "\n\nNEXT STEP: " + nxt
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
            try:
                # The archive came back, so the model wanted the original: count it once
                # against the tool that produced it. An id we never handed out counts nothing.
                origin = self._spill_origin.get(got.get("id"))
                if origin and origin in self.stats["recalls"] and got.get("id") not in self._recalled:
                    self._recalled.add(got.get("id"))
                    self.stats["recalls"][origin]["recalled"] += 1
            except Exception:  # a counter must never cost the caller its archive
                pass
            tail = ""
            if got.get("truncated"):
                tail = ("\n\nNEXT STEP: more remains -- use retrieve_spill again with id %s and start_line %d, "
                        "or narrow it with a grep regex."
                        % (got["id"], got["start_line"] + got["returned_lines"]))
            return ("[%s: lines %d-%d of %d]\n%s%s"
                    % (got["id"], got["start_line"], got["start_line"] + max(got["returned_lines"] - 1, 0),
                       got["total_lines"], got["text"], tail))
        return "unknown router tool %r\n\nNEXT STEP: list_capabilities()" % name

    def _handle_skill(self, name: str, args: dict) -> str:
        """Skills budget: the same catalogue-on-demand shape the
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
                         "find the right tool, then call_tool. To find a tool by "
                         "keyword instead, call search_capabilities(query)."),
         "inputSchema": {"type": "object", "properties": {
             "server": {"type": "string", "description": "server name, or omit for all"}}}},
        {"name": "search_capabilities",
         "description": ("Find a tool by keyword across every connected server. "
                         "Cheaper than listing them all when you know roughly what "
                         "you are looking for."),
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"},
             "server": {"type": "string", "description": "optional: search one server only"},
             "limit": {"type": "integer", "description": "max results, default 8"}},
             "required": ["query"]}},
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


CORE_TOOLS = ("list_capabilities", "describe_tool", "call_tool")


def build_all_tools(backends: dict) -> list:
    """The three router tools the model always needs, FIRST and in that fixed
    order, then search_capabilities and the extras, plus the two skill tools when
    skills are configured. The core three lead because their positions are load
    bearing: the bridge, the self-tests and the pre-0.3.0 orderings all read
    names[:3], and a stable leading triple is what keeps a long session's prompts
    from reshuffling every time a tool is added."""
    tools = build_router_tools(backends)
    head = [t for n in CORE_TOOLS for t in tools if t["name"] == n]
    tail = [t for t in tools if t["name"] not in CORE_TOOLS]
    return (head + tail + build_extra_tools()
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
            client = ((msg.get("params") or {}).get("clientInfo") or {}).get("name") or ""
            router.log_session_start("mcp", str(client))
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
          "\n'Model requirement' section. After a few real tasks,"
          "\n`tool-guardian --bypass-summary` shows whether it did.")
    print("\n" + update_line())
    return 0 if ok else 1


def update_line() -> str:
    """One line: is there a newer release? Offline, opted out or tg_update missing all say so."""
    if tg_update is None:
        return "update: check unavailable (tg_update.py is not beside tool_guardian.py)"
    try:
        return tg_update.status_line(tg_update.check(tg_update.install_kind(py_version=__version__)))
    except Exception:  # noqa: BLE001  (a courtesy line never fails a selftest)
        return "update: could not check -- you have %s" % __version__


def bypass_summary(session: str = "", since_hours: float = 24.0, log_path: str = "") -> str:
    """Router use vs shell bypasses from the call log. Default: the last 24 hours."""
    if tg_state is None:
        return "bypass summary unavailable: tg_state.py is not beside tool_guardian.py"
    path = log_path or CALL_LOG
    if not path:
        return "the call log is off (TOOL_GUARDIAN_CALL_LOG is empty) -- nothing to summarise"
    rows = tg_state.read_calls(path)
    if session == "last":
        session = tg_state.last_session(rows)
    if session and session != "all":
        summary = tg_state.summarize(rows, session=session)
    elif session == "all":
        summary = tg_state.summarize(rows)
    else:
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - since_hours * 3600))
        summary = tg_state.summarize(rows, since=since)
    return "call log: %s\n%s" % (path, tg_state.render_bypass_summary(summary))


def hook_pretooluse(stdin_text: str, mode: str = "") -> tuple:
    """Claude Code PreToolUse hook. Never fails the user's shell call: any problem is exit 0."""
    if tg_state is None:
        return 0, ""
    mode = mode or os.environ.get("TOOL_GUARDIAN_HOOK_MODE", "log")
    return tg_state.run_hook(stdin_text, log_path=CALL_LOG, mode=mode)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tool Guardian -- an MCP router that "
                                 "keeps tool definitions from filling the context window")
    ap.add_argument("--config", default="", help="path to an mcpServers JSON config")
    ap.add_argument("--selftest", action="store_true",
                    help="start backends, print the token saving, exit")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--bypass-summary", dest="bypass_summary", action="store_true",
                    help="router calls vs shell bypasses from the call log, then exit")
    ap.add_argument("--session", default="",
                    help="with --bypass-summary: a session id, 'last' or 'all' (default: last 24 hours)")
    ap.add_argument("--since-hours", dest="since_hours", type=float, default=24.0,
                    help="with --bypass-summary: how far back to look (default 24)")
    ap.add_argument("--hook-pretooluse", dest="hook", action="store_true",
                    help="Claude Code PreToolUse hook: read the hook JSON on stdin, log a bypass")
    ap.add_argument("--mode", default="", choices=["", "log", "deny"],
                    help="with --hook-pretooluse: log (default) or deny ($TOOL_GUARDIAN_HOOK_MODE)")
    ap.add_argument("--skills-report", dest="skills_report", action="store_true",
                    help="print the token saving for skills configured in "
                         "TOOL_GUARDIAN_SKILLS and exit")
    a = ap.parse_args(argv)
    if a.hook:
        # First, and before .env loading: a hook runs on every shell call and must stay fast.
        try:
            code, out = hook_pretooluse(sys.stdin.read(), a.mode)
        except Exception:  # noqa: BLE001  (never break the user's shell)
            code, out = 0, ""
        if out:
            print(out)
        return code
    if a.bypass_summary:
        print(bypass_summary(a.session, a.since_hours))
        return 0
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