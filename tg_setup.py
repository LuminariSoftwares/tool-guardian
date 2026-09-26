# @job: tg-setup-doctor-v1
# @job: tg-setup-add-v1
"""
tg_setup.py
===========
Setup helper for tool-guardian: add an MCP server in one command, or import the
servers you ALREADY have from another MCP client's config, then tell the user
plainly what is configured, what is wrong, and how to fix each thing.

    tool-guardian-setup add <name> [--env KEY=VALUE]... [--description TEXT]
                            [--config PATH] [--replace] -- <command> [args...]
    tool-guardian-setup remove <name> [--config PATH]
    tool-guardian-setup list [--config PATH] [--json]
    tool-guardian-setup import [--from PATH] [--to PATH] [--yes]
    tool-guardian-setup doctor [--config PATH] [--json]

New users must otherwise hand-write an ``mcpServers`` JSON with no validation,
and a typo fails silently. ``add`` is the one-command path: everything after
``--`` is the server's command and its args, verbatim. ``import`` copies servers
from the config a client they already use; ``list`` shows what is configured and
which group each server's tools land in; ``doctor`` reports each problem with a
one-line fix.

The helper functions are pure-ish -- no prompts, no printing, paths and env
injected -- so they are directly testable. The CLI wires them together and owns
all the printing and prompting.

MIT licensed. Standard library only, Python >= 3.9, cross-platform.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import tg_env
import tg_groups
import tool_guardian


# The needles that mean "this server IS tool-guardian". Substring, case-insensitive.
_SELF_NEEDLES = ("tool-guardian", "tool_guardian", "dsh-tool-guardian")
# ${NAME} references inside server args.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The last line of a successful add/remove: the entry is on disk, but a running
# MCP client only re-reads its config when it starts.
RESTART_LINE = "restart your MCP client (or start a new DSH session) to load it"
# Where to look for a config that does not exist yet.
ADD_HINT = "tool-guardian-setup add <name> -- <command> [args...]"


# --------------------------------------------------------------- helpers ----

def client_config_candidates(home: Path, env: dict, cwd: Path) -> list[Path]:
    """Existing client-config files to import FROM, in preference order.

    Claude Desktop (APPDATA, macOS Library, Linux ~/.config) first, then the
    project-local and editor configs. Only paths that exist are returned.
    """
    home = Path(home)
    cwd = Path(cwd)
    appdata = env.get("APPDATA") or ""
    ordered = []
    if appdata:
        ordered.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    ordered.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    ordered.append(home / ".config" / "Claude" / "claude_desktop_config.json")
    ordered.append(cwd / ".mcp.json")
    ordered.append(cwd / "mcp.json")
    ordered.append(home / ".cursor" / "mcp.json")
    ordered.append(home / ".codeium" / "windsurf" / "mcp_config.json")
    return [p for p in ordered if p.is_file()]


def read_servers(path: Path) -> dict:
    """{name: spec} from a file's ``mcpServers`` object.

    Missing/unreadable file, invalid JSON, or a missing/non-object mcpServers
    all raise ValueError with a plain-English message naming the file.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("could not read %s: %s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("%s is not valid JSON (line %d, column %d): %s"
                         % (path, exc.lineno, exc.colno, exc.msg)) from exc
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % path)
    servers = data.get("mcpServers")
    if servers is None:
        raise ValueError('%s has no "mcpServers" object' % path)
    if not isinstance(servers, dict):
        raise ValueError('%s: "mcpServers" is not an object' % path)
    return servers


def is_self(spec) -> bool:
    """True when the spec IS tool-guardian (by command or any arg)."""
    if not isinstance(spec, dict):
        return False
    parts = [str(spec.get("command") or "")]
    parts.extend(str(a) for a in (spec.get("args") or []))
    hay = " ".join(parts).lower()
    return any(n in hay for n in _SELF_NEEDLES)


def _skip_reason(spec):
    """Why this server cannot be imported, or None when it can."""
    if not isinstance(spec, dict):
        return "spec is not a JSON object"
    if spec.get("url") and not spec.get("command"):
        return "HTTP/SSE server: not supported yet — keep it in your client directly"
    if is_self(spec):
        return "this is tool-guardian itself"
    if not spec.get("command"):
        return "no command"
    return None


def plan_import(source: dict, existing: dict | None) -> dict:
    """Decide what importing ``source`` into ``existing`` does -- WITHOUT mutating
    either input.

    Returns {"servers": merged, "added": [...], "kept_existing": [...],
    "skipped": [{"name", "why"}]}. An existing server always wins over a source
    one with the same name. Specs (env/args/description) are copied unchanged.
    """
    existing = existing or {}
    merged = dict(existing)
    added, kept_existing, skipped = [], [], []
    for name, spec in source.items():
        why = _skip_reason(spec)
        if why is not None:
            skipped.append({"name": name, "why": why})
            continue
        if name in existing:
            kept_existing.append(name)
            merged[name] = existing[name]
        else:
            added.append(name)
            merged[name] = spec
    return {"servers": merged, "added": added, "kept_existing": kept_existing,
            "skipped": skipped}


def write_config(path: Path, servers: dict, clock=time.time, extra: dict | None = None) -> Path | None:
    """Write ``{"mcpServers": servers}`` atomically to ``path`` (indent 2, utf-8,
    trailing newline), creating parent dirs.

    If a file was already there it is first copied to ``<path>.bak-<stamp>``
    (``-1``, ``-2``... appended when that name is taken) and that backup path is
    returned; otherwise None. ``clock`` is injected so tests get a deterministic
    backup name. ``extra`` puts the file's other top-level keys (``toolGuardian``
    and friends) back beside mcpServers; without it the output is exactly what it
    always was, so existing callers are unaffected.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
        backup = Path(str(path) + ".bak-" + stamp)
        tries = 0
        while backup.exists():
            tries += 1
            backup = Path(f"{path}.bak-{stamp}-{tries}")
        shutil.copy2(path, backup)
    doc = dict(extra or {})
    doc["mcpServers"] = servers
    data = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return backup


def client_snippet() -> dict:
    """The tiny block a user's MCP client needs: tool-guardian and nothing else."""
    return {"mcpServers": {"tool-guardian": {"command": "tool-guardian", "args": []}}}


def _find_env_file(anchor: Path | None, environ: dict) -> Path | None:
    """Locate a .env the way tg_env does: explicit TOOL_GUARDIAN_ENV, else an
    upward search from the config file's directory (or cwd). None when absent."""
    explicit = environ.get("TOOL_GUARDIAN_ENV")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    start = Path(anchor).parent if anchor else Path.cwd()
    d = start
    for _ in range(6):
        cand = d / ".env"
        if cand.is_file():
            return cand
        if d.parent == d:
            break
        d = d.parent
    return None


def target_config(explicit: str = "", env: dict | None = None) -> Path:
    """The config file add/remove/list work on: the FIRST existing path in the
    router's own ``_config_search_order`` -- the very file ``doctor`` reports on.

    When none of them exists yet the file is still chosen: the explicit path if
    given, else ``$TOOL_GUARDIAN_CONFIG``, else ``~/.tool-guardian/mcp.json``. So
    ``add`` writes somewhere predictable instead of failing on a fresh machine.
    """
    if env is None:
        env = dict(os.environ)
    for path in tool_guardian._config_search_order(explicit):
        try:
            if path.is_file():
                return Path(path)
        except OSError:
            continue
    if explicit:
        return Path(explicit)
    from_env = (env.get("TOOL_GUARDIAN_CONFIG") or "").strip()
    if from_env:
        return Path(from_env)
    return Path.home() / ".tool-guardian" / "mcp.json"


def _load_for_edit(path: Path) -> tuple[dict | None, str]:
    """(document, "") for a missing file ({}) or a well-formed one; (None, reason) when the file EXISTS but is not a
    JSON object with an object ``mcpServers`` (or none). add/remove/list refuse such a file instead of treating it as
    empty: a typo in a hand-edited config must never cost the user every server in it."""
    path = Path(path)
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"could not read {path}: {exc}"
    except ValueError as exc:
        return None, f"{path} is not valid JSON ({exc})"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    if "mcpServers" in data and not isinstance(data["mcpServers"], dict):
        return None, f'{path}: "mcpServers" is not an object'
    return data, ""


def _refuse_broken(reason: str) -> int:
    print(reason)
    print("nothing was changed -- fix the file first; `tool-guardian-setup doctor` shows where")
    return 1


def _document_parts(doc: dict) -> tuple[dict, dict]:
    """(mcpServers, the other top-level keys) -- the second half is what add and
    remove must put back untouched, e.g. the ``toolGuardian`` groups block."""
    servers = doc.get("mcpServers")
    return (servers if isinstance(servers, dict) else {},
            {k: v for k, v in doc.items() if k != "mcpServers"})


def group_for_server(name: str, groups_cfg: dict | None) -> str:
    """Which group tool-guardian will show this server's tools in -- decided from
    the config alone, no server started.

    Mirrors ``tg_groups._members``: no groups configured means one group per
    server, so the server is its own group. Otherwise the first group in config
    order whose selectors name this server (bare name, or a ``name.tool*``
    prefix), and a server no selector names goes to the uncovered-tools group,
    named exactly as ``_members`` names it.
    """
    if not groups_cfg:
        return name
    for group, selectors in groups_cfg.items():
        for selector in selectors or []:
            sel = str(selector)
            if sel == name or sel.startswith(name + "."):
                return group
    return tg_groups.uncovered_group_name(groups_cfg)


# What to install for a command that is not on PATH.
_INSTALL_FIX = {
    "npx": "install Node.js (https://nodejs.org) — npx ships with it, then reopen your shell",
    "uvx": "install uv (https://docs.astral.sh/uv/) — e.g. `pip install uv`, or the standalone installer",
    "node": "install Node.js (https://nodejs.org)",
    "python": "install Python 3 (https://python.org) and make sure `python` is on PATH",
    "python3": "install Python 3 (https://python.org) and make sure `python3` is on PATH",
    "docker": "install Docker (https://docs.docker.com/get-docker/) and make sure the docker CLI is on PATH",
}


def _fix_for_missing_command(command: str) -> str:
    return _INSTALL_FIX.get(command, "install %r and make sure it is on PATH" % command)


def doctor(explicit: str = "", env: dict | None = None, which=shutil.which) -> list[dict]:
    """Check the setup and return findings in order. Each finding is
    {"level": "ok"|"warn"|"error", "what": str, "fix": str}; "fix" is "" for ok.

    ``env`` and ``which`` are injected so the checks never touch the real
    environment or PATH.
    """
    if env is None:
        env = dict(os.environ)
    work = dict(env)  # .env is loaded into a COPY; os.environ is never touched.

    # 1. config: first existing path from the router's own search order.
    found = None
    for path in tool_guardian._config_search_order(explicit):
        try:
            if path.is_file():
                found = path
                break
        except OSError:
            continue
    findings = []
    if found is None:
        findings.append({
            "level": "error",
            "what": "no config found",
            "fix": "run: tool-guardian-setup import   (or create ~/.tool-guardian/mcp.json)",
        })
        env_path = _find_env_file(None, work)
        findings.append(_env_finding(env_path, work))
        return findings

    findings.append({"level": "ok", "what": "config: %s" % found, "fix": ""})

    # 2. JSON parses and has mcpServers.
    try:
        servers = read_servers(found)
    except ValueError as exc:
        findings.append({"level": "error", "what": str(exc), "fix":
                         "fix the JSON (trailing comma? missing quote?) and re-run: tool-guardian-setup doctor"})
        env_path = _find_env_file(found, work)
        findings.append(_env_finding(env_path, work))
        return findings

    findings.append({"level": "ok", "what": "config parses: %d server(s) in mcpServers"
                     % len(servers), "fix": ""})

    # 3. per-server checks.
    findings.extend(_server_findings(servers, which))

    # 4. .env presence.
    env_path = _find_env_file(found, work)
    tg_env.load_env_file(str(env_path) if env_path else None, environ=work)
    findings.append(_env_finding(env_path, work))

    # 5. ${VAR} references whose VAR is unset in env+.env.
    unset = _unset_var_refs(servers, work)
    for var in unset:
        findings.append({
            "level": "warn",
            "what": 'args reference ${%s} but it is not set in the environment or .env' % var,
            "fix": "add %s=... to your .env" % var,
        })
    return findings


def _server_findings(servers: dict, which, report_found: bool = False) -> list[dict]:
    """Per-server findings. With ``report_found`` a command that IS on PATH also
    gets an "ok" line -- that is what ``add`` wants for the one server it just
    wrote. ``doctor`` leaves it off and its output is unchanged."""
    out = []
    if not servers:
        out.append({"level": "warn", "what": "no servers configured",
                    "fix": "run: tool-guardian-setup import   (or add servers to mcp.json)"})
        return out
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            out.append({"level": "error", "what": 'server %r: spec is not an object' % name,
                        "fix": "fix or remove this entry"})
            continue
        if spec.get("url") and not spec.get("command"):
            out.append({"level": "warn", "what": 'server %r: HTTP/SSE not supported yet' % name,
                        "fix": "keep it in your client directly — tool-guardian speaks stdio only"})
            continue
        if is_self(spec):
            out.append({
                "level": "error",
                "what": "tool-guardian lists itself (server %r) — remove it from this file "
                        "(it belongs in your client's config)" % name,
                "fix": "remove the %r entry from this config; tool-guardian is the router, "
                       "it is configured in your MCP client, not here" % name,
            })
            # An is_self server is not a real backend -- do not also which-check it.
            continue
        command = spec.get("command")
        if not command:
            continue
        if which(command) is None:
            out.append({
                "level": "error",
                "what": "command %r not found on PATH" % command,
                "fix": _fix_for_missing_command(str(command)),
            })
        elif report_found:
            out.append({"level": "ok", "what": f"server {name!r}: command {command!r} found",
                        "fix": ""})
    return out


def _env_finding(env_path: Path | None, work: dict) -> dict:
    if env_path is not None and env_path.is_file():
        return {"level": "ok", "what": "using .env: %s" % env_path, "fix": ""}
    return {"level": "ok", "what": "no .env (optional)", "fix": ""}


def _unset_var_refs(servers: dict, work: dict) -> list[str]:
    """${VAR} names in any server's args that are unset in work (env + .env)."""
    missing = []
    for spec in servers.values():
        if not isinstance(spec, dict):
            continue
        for arg in (spec.get("args") or []):
            if isinstance(arg, str):
                for name in _VAR_RE.findall(arg):
                    if name not in work and name not in missing:
                        missing.append(name)
    return missing


def _unset_var_refs_with_env(servers: dict, work: dict) -> list[str]:
    """Same as ``_unset_var_refs`` but ALSO scans env values. ``--env TOKEN=${X}``
    is just as broken as ``-- --token=${X}`` -- the server is started with the
    literal ``${X}`` either way -- and doctor's own check is left untouched."""
    missing = []
    for spec in servers.values():
        if not isinstance(spec, dict):
            continue
        texts = [a for a in (spec.get("args") or []) if isinstance(a, str)]
        env = spec.get("env")
        if isinstance(env, dict):
            texts.extend(str(v) for v in env.values())
        for text in texts:
            for name in _VAR_RE.findall(text):
                if name not in work and name not in missing:
                    missing.append(name)
    return missing


def _added_server_findings(name: str, spec: dict, work: dict, which) -> list[dict]:
    """Doctor findings for ONE server -- the one just written -- built from
    doctor's own helpers so the wording matches. ``work`` is env + .env, already
    loaded into a copy; findings for any other server in the file never appear."""
    findings = _server_findings({name: spec}, which, report_found=True)
    for var in _unset_var_refs_with_env({name: spec}, work):
        findings.append({
            "level": "warn",
            "what": f"args/env reference ${{{var}}} but it is not set in the environment or .env",
            "fix": f"add {var}=... to your .env",
        })
    return findings


def _print_findings(findings) -> None:
    """The doctor report format: one line per finding, the fix indented under it."""
    for f in findings:
        if f["level"] == "ok":
            print("OK   %s" % f["what"])
        else:
            tag = "WARN" if f["level"] == "warn" else "FAIL"
            print("%s %s" % (tag, f["what"]))
            print("     fix: %s" % f["fix"])


# ------------------------------------------------------------------- CLI ----

ADD_USAGE = ("usage: tool-guardian-setup add <name> [--env KEY=VALUE]... "
             "[--description TEXT] [--config PATH] [--replace] -- <command> [args...]")


def _usage_error(message: str) -> int:
    """A bad command line. Exit 2, nothing written, message on stderr."""
    print("usage error: " + message, file=sys.stderr)
    print(ADD_USAGE, file=sys.stderr)
    return 2


def _env_pairs(items) -> tuple[dict, str]:
    """{KEY: VALUE} from repeated --env KEY=VALUE, split on the FIRST '='. The
    bad item is returned alongside so the caller can put it in the error."""
    pairs = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            return pairs, item
        pairs[key] = value
    return pairs, ""


def _cmd_add(argv) -> int:
    if "--" not in argv:
        return _usage_error("the server's command goes after --, e.g. " + ADD_HINT)
    cut = argv.index("--")
    head, tail = argv[:cut], argv[cut + 1:]
    if not tail:
        return _usage_error("no command after --")

    ap = argparse.ArgumentParser(prog="tool-guardian-setup add", add_help=True)
    ap.add_argument("name")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--description", default="", metavar="TEXT")
    ap.add_argument("--config", default="", help="a specific mcpServers JSON to write")
    ap.add_argument("--replace", action="store_true", help="overwrite an existing entry")
    a = ap.parse_args(head)  # a bad option exits 2 by itself, which is what we want

    env, bad = _env_pairs(a.env)
    if bad:
        return _usage_error(f"--env wants KEY=VALUE, got {bad!r}")

    # Everything after -- is the command then its args, verbatim: leading '-'
    # included, so argparse never gets to reinterpret it.
    spec = {"command": tail[0], "args": list(tail[1:])}
    if env:
        spec["env"] = env
    if a.description:
        spec["description"] = a.description

    target = target_config(a.config)
    doc, broken = _load_for_edit(target)
    if doc is None:
        return _refuse_broken(broken)
    servers, extra = _document_parts(doc)
    if a.name in servers and not a.replace:
        print(f"server {a.name!r} already exists in {target} -- use --replace to overwrite")
        return 1
    if is_self(spec):
        print(f"refusing to add {a.name!r}: that spec is tool-guardian itself")
        print("tool-guardian is the router -- it belongs in your MCP client's config, not here")
        return 1

    merged = dict(servers)
    merged[a.name] = spec
    backup = write_config(target, merged, extra=extra)
    print(f"added {a.name!r} to {target}")
    if backup is not None:
        print(f"backup: {backup}")

    # Doctor just the server we wrote, so the user is told about THIS entry and
    # nothing else. .env goes into a copy; os.environ is never touched.
    work = dict(os.environ)
    env_path = _find_env_file(target, work)
    tg_env.load_env_file(str(env_path) if env_path else None, environ=work)
    _print_findings(_added_server_findings(a.name, spec, work, shutil.which))
    print(RESTART_LINE)
    return 0


def _cmd_remove(argv) -> int:
    ap = argparse.ArgumentParser(prog="tool-guardian-setup remove", add_help=True)
    ap.add_argument("name")
    ap.add_argument("--config", default="", help="a specific mcpServers JSON to edit")
    a = ap.parse_args(argv)

    target = target_config(a.config)
    if not target.is_file():
        print("no config found -- add one with: " + ADD_HINT)
        return 1
    doc, broken = _load_for_edit(target)
    if doc is None:
        return _refuse_broken(broken)
    servers, extra = _document_parts(doc)
    if a.name not in servers:
        print(f"server {a.name!r} is not in {target}")
        return 1

    merged = dict(servers)
    del merged[a.name]
    backup = write_config(target, merged, extra=extra)
    print(f"removed {a.name!r} from {target}")
    if backup is not None:
        print(f"backup: {backup}")
    print(RESTART_LINE)
    return 0


def _cmd_list(argv) -> int:
    ap = argparse.ArgumentParser(prog="tool-guardian-setup list", add_help=True)
    ap.add_argument("--config", default="", help="a specific mcpServers JSON to read")
    ap.add_argument("--json", action="store_true", help="print one JSON object per server")
    a = ap.parse_args(argv)

    target = target_config(a.config)
    if not target.is_file():
        print("no config found -- add one with: " + ADD_HINT)
        return 1
    doc, broken = _load_for_edit(target)
    if doc is None:
        return _refuse_broken(broken)
    servers, _ = _document_parts(doc)
    options = doc.get("toolGuardian")
    groups_cfg = (options.get("groups") if isinstance(options, dict) else None) or {}
    rows = [{"name": name, "command": str(spec.get("command") or ""),
             "group": group_for_server(name, groups_cfg),
             "description": str(spec.get("description") or "")}
            for name, spec in servers.items() if isinstance(spec, dict)]

    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"no servers in {target}")
        return 0
    w_name = max(len(r["name"]) for r in rows)
    w_cmd = max(len(r["command"]) for r in rows)
    for r in rows:
        print(f"{r['name']:<{w_name}}  {r['command']:<{w_cmd}}  {r['group']}")
    print(f"{len(rows)} server(s) in {target}")
    return 0


def _cmd_import(argv) -> int:
    ap = argparse.ArgumentParser(prog="tool-guardian-setup import", add_help=True)
    ap.add_argument("--from", dest="src", default="", help="client config to read")
    ap.add_argument("--to", dest="dst", default="", help="tool-guardian config to write")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    a = ap.parse_args(argv)

    home = Path.home()
    env = dict(os.environ)
    cwd = Path.cwd()
    src = Path(a.src) if a.src else None
    if src is None:
        candidates = client_config_candidates(home, env, cwd)
        if not candidates:
            print("No MCP client config found to import from.")
            print("Looked for a Claude Desktop, Cursor, or Windsurf config, and ./.mcp.json.")
            print("Write one by hand instead, e.g.:")
            print(json.dumps({"mcpServers": {"files": {
                "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]}}},
                indent=2))
            return 1
        src = candidates[0]
    print("Reading %s" % src)

    try:
        source = read_servers(src)
    except ValueError as exc:
        print(exc)
        return 1

    dst = Path(a.dst) if a.dst else home / ".tool-guardian" / "mcp.json"
    existing = None
    if dst.is_file():
        try:
            existing = read_servers(dst)
        except ValueError:
            existing = None  # unreadable dest is backed up before being replaced

    plan = plan_import(source, existing)
    for name in plan["added"]:
        print("  + %s (add)" % name)
    for name in plan["kept_existing"]:
        print("  = %s (keep existing)" % name)
    for skip in plan["skipped"]:
        print("  - %s: %s" % (skip["name"], skip["why"]))
    if not plan["added"] and not plan["kept_existing"]:
        print("  (nothing to import)")

    count = len(plan["servers"])
    if not a.yes:
        try:
            answer = input("Write %d servers to %s? [y/N] " % (count, dst))
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing written.")
            return 0

    backup = write_config(dst, plan["servers"])
    if backup is not None:
        print("Backed up the existing config to %s" % backup)
    print("Wrote %d server(s) to %s" % (count, dst))
    print("Now point your MCP client at tool-guardian only:")
    print(json.dumps(client_snippet(), indent=2))
    print("and remove the servers you just imported from that client's own config, or they load twice.")
    return 0


def _cmd_doctor(argv) -> int:
    ap = argparse.ArgumentParser(prog="tool-guardian-setup doctor", add_help=True)
    ap.add_argument("--config", default="", help="a specific mcpServers JSON to check")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    a = ap.parse_args(argv)
    findings = doctor(explicit=a.config)
    if a.json:
        print(json.dumps(findings, indent=2))
        return 1 if any(f["level"] == "error" for f in findings) else 0
    ok = sum(1 for f in findings if f["level"] == "ok")
    warns = sum(1 for f in findings if f["level"] == "warn")
    errors = sum(1 for f in findings if f["level"] == "error")
    _print_findings(findings)
    print("doctor: %d ok, %d warnings, %d errors" % (ok, warns, errors))
    return 1 if errors else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.split("\n\n")[0])
        print("\nusage: tool-guardian-setup {add|remove|list|import|doctor} [options] | --selftest")
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "add":
        return _cmd_add(rest)
    if sub == "remove":
        return _cmd_remove(rest)
    if sub == "list":
        return _cmd_list(rest)
    if sub == "import":
        return _cmd_import(rest)
    if sub == "doctor":
        return _cmd_doctor(rest)
    print(f"unknown command {sub!r} — expected \"add\", \"remove\", \"list\", "
          "\"import\" or \"doctor\"")
    return 1


# -------------------------------------------------------------- selftest ----

@contextlib.contextmanager
def _hermetic():
    """Point HOME, cwd, and TOOL_GUARDIAN_CONFIG at a throwaway empty dir so a
    check can never read the real home, PATH, or a stray mcp.json. Restored on exit."""
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "TOOL_GUARDIAN_CONFIG")}
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        os.environ["HOME"] = os.environ["USERPROFILE"] = tmp
        os.environ.pop("TOOL_GUARDIAN_CONFIG", None)
        os.chdir(tmp)
        yield Path(tmp)
    finally:
        os.chdir(cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _fake_which(*ok):
    def which(command):
        return "/usr/bin/" + command if command in ok else None
    return which


@contextlib.contextmanager
def _patch_input(answer):
    """Inject the answer the confirmation prompt() will see."""
    import builtins
    saved = builtins.input
    builtins.input = lambda _="": answer
    try:
        yield
    finally:
        builtins.input = saved


def _check_candidates():
    with _hermetic() as root:
        home = root / "home"
        cwd = root / "cwd"
        appdata = root / "appdata"
        for d in (home, cwd, appdata):
            d.mkdir(parents=True, exist_ok=True)
        # existing: APPDATA Claude, macOS Library, project .mcp.json, cursor
        _write(appdata / "Claude" / "claude_desktop_config.json", {"mcpServers": {}})
        _write(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
               {"mcpServers": {}})
        _write(cwd / ".mcp.json", {"mcpServers": {}})
        _write(home / ".cursor" / "mcp.json", {"mcpServers": {}})
        # deliberately NOT created: linux ~/.config, cwd/mcp.json, windsurf

        env = {"APPDATA": str(appdata)}
        got = client_config_candidates(home, env, cwd)
        want = [appdata / "Claude" / "claude_desktop_config.json",
                home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
                cwd / ".mcp.json",
                home / ".cursor" / "mcp.json"]
        assert got == want, "order/only-existing wrong:\n got=%s\nwant=%s" % (got, want)

        # Without APPDATA in env the first candidate is dropped.
        got2 = client_config_candidates(home, {}, cwd)
        assert want[0] not in got2, "APPDATA candidate present without APPDATA in env"


def _check_read_servers_bad_json():
    with _hermetic() as root:
        p = root / "broken.json"
        p.write_text('{"mcpServers": {oops', encoding="utf-8")
        try:
            read_servers(p)
        except ValueError as exc:
            assert str(p) in str(exc), "message must name the file: %s" % exc
        else:
            raise AssertionError("bad JSON should raise ValueError")

        # valid file round-trips
        good = _write(root / "ok.json", {"mcpServers": {"a": {"command": "node"}}})
        assert read_servers(good) == {"a": {"command": "node"}}

        # missing mcpServers raises and names the file
        no = _write(root / "no.json", {"other": 1})
        try:
            read_servers(no)
        except ValueError as exc:
            assert str(no) in str(exc)
        else:
            raise AssertionError("missing mcpServers should raise ValueError")


def _check_plan_import():
    with _hermetic():
        source = {
            "web": {"url": "http://example.com/sse"},
            "tg": {"command": "tool-guardian", "args": []},
            "bad": {"description": "no command here"},
            "dup": {"command": "newnode", "args": ["n"]},
            "fresh": {"command": "python", "args": ["p"], "env": {"A": "B"}, "description": "d"},
        }
        existing = {"dup": {"command": "oldnode", "args": ["old"]}}
        src_before = json.dumps(source, sort_keys=True)
        exi_before = json.dumps(existing, sort_keys=True)
        plan = plan_import(source, existing)
        assert json.dumps(source, sort_keys=True) == src_before, "source mutated"
        assert json.dumps(existing, sort_keys=True) == exi_before, "existing mutated"

        whys = {s["name"]: s["why"] for s in plan["skipped"]}
        assert whys.get("web") == ("HTTP/SSE server: not supported yet — "
                                    "keep it in your client directly"), whys
        assert whys.get("tg") == "this is tool-guardian itself", whys
        assert whys.get("bad") == "no command", whys
        assert "dup" not in plan["added"] and "dup" in plan["kept_existing"], plan
        assert plan["added"] == ["fresh"], plan["added"]
        # existing wins
        assert plan["servers"]["dup"]["command"] == "oldnode", plan["servers"]["dup"]
        # spec copied unchanged, env/description preserved
        assert plan["servers"]["fresh"] == source["fresh"], plan["servers"]["fresh"]
        # skipped names are not merged
        for name in ("web", "tg", "bad"):
            assert name not in plan["servers"], name


def _check_write_config():
    with _hermetic() as root:
        dest = root / "nested" / "dir" / "mcp.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text('{"mcpServers": {"old": {"command": "old"}}}', encoding="utf-8")
        servers = {"a": {"command": "node", "args": ["x"]}}
        backup = write_config(dest, servers, clock=lambda: 0.0)
        assert backup is not None, "existing file should produce a backup"
        assert str(backup).startswith(str(dest) + ".bak-"), backup
        assert json.loads(backup.read_text(encoding="utf-8")) == {"mcpServers": {"old": {"command": "old"}}}

        raw = dest.read_text(encoding="utf-8")
        assert raw == json.dumps({"mcpServers": servers}, indent=2) + "\n", "content/trailing newline wrong"
        # atomic: no temp files left behind
        leftovers = [p.name for p in dest.parent.iterdir() if p.suffix == ".tmp"]
        assert not leftovers, "temp files left: %s" % leftovers

        # no existing file -> no backup
        fresh = root / "fresh" / "mcp.json"
        assert write_config(fresh, servers, clock=lambda: 0.0) is None


def _check_doctor_no_config():
    with _hermetic():
        findings = doctor(explicit="", env={}, which=_fake_which())
        assert any(f["level"] == "error" and f["what"] == "no config found" for f in findings), findings
        err = [f for f in findings if f["what"] == "no config found"][0]
        assert "tool-guardian-setup import" in err["fix"], err["fix"]


def _check_doctor_missing_command():
    with _hermetic() as root:
        cfg = _write(root / "mcp.json", {"mcpServers": {"web": {
            "command": "npx", "args": ["-y", "pkg"]}}})
        findings = doctor(explicit=str(cfg), env={}, which=_fake_which())  # npx NOT in ok
        errs = [f for f in findings if f["level"] == "error"]
        assert any("command 'npx' not found on PATH" in f["what"] for f in errs), errs
        miss = [f for f in errs if "not found on PATH" in f["what"]][0]
        assert "Node" in miss["fix"], miss["fix"]


def _check_doctor_lists_itself():
    with _hermetic() as root:
        cfg = _write(root / "mcp.json", {"mcpServers": {
            "tool-guardian": {"command": "tool-guardian", "args": []}}})
        # which returns a path for tool-guardian so ONLY the self-reference errors.
        findings = doctor(explicit=str(cfg), env={}, which=_fake_which("tool-guardian"))
        errs = [f for f in findings if f["level"] == "error"]
        assert any("lists itself" in f["what"] for f in errs), errs
        assert len(errs) == 1, "expected exactly one error, got %s" % errs


def _check_doctor_unset_var():
    with _hermetic() as root:
        cfg = _write(root / "mcp.json", {"mcpServers": {"api": {
            "command": "node", "args": ["run", "--token=${SECRET}"]}}})
        findings = doctor(explicit=str(cfg), env={}, which=_fake_which("node"))
        warns = [f for f in findings if f["level"] == "warn"]
        assert any("SECRET" in f["what"] for f in warns), warns
        w = [f for f in warns if "SECRET" in f["what"]][0]
        assert "add SECRET=... to your .env" == w["fix"], w["fix"]


def _check_doctor_all_good():
    with _hermetic() as root:
        cfg = _write(root / "mcp.json", {"mcpServers": {
            "files": {"command": "node", "args": ["server.js"], "description": "files"}}})
        findings = doctor(explicit=str(cfg), env={}, which=_fake_which("node"))
        errors = [f for f in findings if f["level"] == "error"]
        assert not errors, "all-good config should have 0 errors, got %s" % errors
        assert any(f["level"] == "ok" and "no .env" in f["what"] for f in findings), findings


def _check_import_yes():
    with _hermetic() as root:
        src = _write(root / "client.json", {"mcpServers": {
            "files": {"command": "node", "args": ["fs.js"]}}})
        dst = root / "out" / "mcp.json"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["import", "--from", str(src), "--to", str(dst), "--yes"])
        out = buf.getvalue()
        assert rc == 0, "import --yes should exit 0, got %s" % rc
        assert dst.is_file(), "dest not written"
        assert json.loads(dst.read_text(encoding="utf-8")) == {
            "mcpServers": {"files": {"command": "node", "args": ["fs.js"]}}}
        assert "Now point your MCP client at tool-guardian only:" in out, out
        assert json.dumps(client_snippet(), indent=2) in out, "snippet not printed:\n%s" % out
        assert "or they load twice" in out, out


def _check_import_no_prompt_writes_nothing():
    with _hermetic() as root:
        src = _write(root / "client.json", {"mcpServers": {
            "files": {"command": "node", "args": ["fs.js"]}}})
        dst = root / "out" / "mcp.json"
        buf = io.StringIO()
        with _patch_input("n"):
            with contextlib.redirect_stdout(buf):
                rc = main(["import", "--from", str(src), "--to", str(dst)])
        assert rc == 0, "declining the prompt should exit 0, got %s" % rc
        assert not dst.exists(), "declining must write nothing, but dest exists"


def _capture(argv):
    """(exit code, stdout) from main(argv) with the printing captured. stderr is
    swallowed so the deliberate usage errors below do not pollute the report."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = main(argv)
    return rc, buf.getvalue()


def _backs(path: Path) -> list[str]:
    d, base = path.parent, path.name
    return [p.name for p in d.iterdir() if p.name.startswith(base + ".bak-")] if d.is_dir() else []


def _check_add_writes_target_and_keeps_other_keys():
    with _hermetic() as root:
        cfg = _write(root / "cfg" / "mcp.json", {
            "mcpServers": {"old": {"command": "node", "args": []}},
            "toolGuardian": {"groups": {"code": ["old"]}}})
        rc, out = _capture(["add", "git", "--config", str(cfg), "--", "node", "-m", "srv"])
        assert rc == 0, out
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert set(data["mcpServers"]) == {"old", "git"}, data
        assert data["mcpServers"]["git"] == {"command": "node", "args": ["-m", "srv"]}, data
        assert data["toolGuardian"] == {"groups": {"code": ["old"]}}, data
        backs = _backs(cfg)
        assert len(backs) == 1, backs
        assert json.loads((cfg.parent / backs[0]).read_text(encoding="utf-8"))["mcpServers"] == \
            {"old": {"command": "node", "args": []}}, "backup must be the OLD file"
        assert f"added 'git' to {cfg}" in out, out
        assert f"backup: {cfg.parent / backs[0]}" in out, out
        assert out.strip().splitlines()[-1] == RESTART_LINE, out


def _check_add_refuses_duplicate_without_replace():
    with _hermetic() as root:
        cfg = _write(root / "cfg" / "mcp.json", {"mcpServers": {"git": {"command": "node", "args": ["a"]}}})
        before = cfg.read_text(encoding="utf-8")
        rc, out = _capture(["add", "git", "--config", str(cfg), "--", "node", "b"])
        assert rc == 1, out
        assert f"server 'git' already exists in {cfg}" in out and "--replace" in out, out
        assert cfg.read_text(encoding="utf-8") == before, "file must be untouched"
        assert _backs(cfg) == [], "a refusal must not back anything up"
        # --replace overwrites that entry and still backs the file up first.
        rc, out = _capture(["add", "git", "--config", str(cfg), "--replace", "--", "node", "b"])
        assert rc == 0, out
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["mcpServers"]["git"]["args"] == ["b"], data
        assert len(_backs(cfg)) == 1, _backs(cfg)


def _check_add_args_after_double_dash_verbatim():
    with _hermetic() as root:
        cfg = root / "cfg" / "mcp.json"
        rc, out = _capture(["add", "fs", "--config", str(cfg), "--description", "files",
                            "--", "node", "-y", "--flag", "/data", "--", "x"])
        assert rc == 0, out
        spec = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["fs"]
        assert spec == {"command": "node", "args": ["-y", "--flag", "/data", "--", "x"],
                        "description": "files"}, spec
        # --env splits on the FIRST '=' only.
        rc, out = _capture(["add", "gh", "--config", str(cfg), "--env", "A=1",
                            "--env", "B=two=2", "--", "node"])
        assert rc == 0, out
        assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["gh"]["env"] == \
            {"A": "1", "B": "two=2"}, out
        # No -- at all, a KEY with no '=', and a bare -- are all usage errors.
        for bad in (["add", "nope", "--config", str(cfg), "node"],
                    ["add", "nope", "--config", str(cfg), "--env", "NOEQUALS", "--", "node"],
                    ["add", "nope", "--config", str(cfg), "--"]):
            rc, _ = _capture(bad)
            assert rc == 2, "expected a usage error (2) for %r, got %r" % (bad, rc)
        assert set(json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]) == {"fs", "gh"}, \
            "a usage error must write nothing"


def _check_add_warns_unset_var_in_env_value():
    with _hermetic() as root:
        cfg = root / "cfg" / "mcp.json"
        (root / ".env").write_text("TG_DOTENV_VAR=here\n", encoding="utf-8")
        # Set in the real environment, in .env, or nowhere -- only the last warns.
        os.environ["TG_PRESENT_VAR"] = "yes"
        rc, out = _capture(["add", "tok", "--config", str(cfg),
                            "--env", "A=${TG_MISSING_VAR}",
                            "--", "node", "--x=${TG_PRESENT_VAR}", "--y=${TG_DOTENV_VAR}"])
        os.environ.pop("TG_PRESENT_VAR", None)
        assert rc == 0, out
        warns = [ln for ln in out.splitlines() if ln.startswith("WARN")]
        assert any("TG_MISSING_VAR" in ln for ln in warns), out
        assert not any("TG_PRESENT_VAR" in ln for ln in warns), out
        assert not any("TG_DOTENV_VAR" in ln for ln in warns), out
        assert "args/env reference ${TG_MISSING_VAR} but it is not set in the environment or .env" \
            in out, out
        assert "fix: add TG_MISSING_VAR=... to your .env" in out, out
        # os.environ is never mutated by the .env lookup.
        assert "TG_DOTENV_VAR" not in os.environ, "load_env_file must not touch os.environ"
        # doctor's own check is unchanged: args only, and different wording.
        assert _unset_var_refs({"a": {"args": ["${TG_MISSING_VAR}"]}}, {}) == ["TG_MISSING_VAR"]
        assert _unset_var_refs({"a": {"args": [], "env": {"K": "${TG_MISSING_VAR}"}}}, {}) == []


def _check_add_refuses_tool_guardian_itself():
    with _hermetic() as root:
        cfg = root / "cfg" / "mcp.json"
        rc, out = _capture(["add", "me", "--config", str(cfg), "--", "tool-guardian"])
        assert rc == 1, out
        assert "itself" in out, out
        assert not cfg.exists(), "nothing written"


def _check_remove_unknown_writes_nothing():
    with _hermetic() as root:
        cfg = _write(root / "cfg" / "mcp.json", {"mcpServers": {"a": {"command": "node"}},
                                                 "toolGuardian": {"x": 1}})
        before = cfg.read_text(encoding="utf-8")
        rc, out = _capture(["remove", "zzz", "--config", str(cfg)])
        assert rc == 1, out
        assert f"server 'zzz' is not in {cfg}" in out, out
        assert cfg.read_text(encoding="utf-8") == before, "nothing written"
        assert _backs(cfg) == [], "no backup on a no-op"

        rc, out = _capture(["remove", "a", "--config", str(root / "cfg" / "absent.json")])
        assert rc == 1 and "no config found" in out, out

        rc, out = _capture(["remove", "a", "--config", str(cfg)])
        assert rc == 0, out
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["mcpServers"] == {} and data["toolGuardian"] == {"x": 1}, data
        assert len(_backs(cfg)) == 1, _backs(cfg)
        assert out.strip().splitlines()[-1] == RESTART_LINE, out


def _check_list_group_default_and_custom_other():
    with _hermetic() as root:
        cfg = root / "cfg" / "mcp.json"

        def rows(servers, groups=None):
            doc = {"mcpServers": servers}
            if groups is not None:
                doc["toolGuardian"] = {"groups": groups}
            _write(cfg, doc)
            rc, out = _capture(["list", "--config", str(cfg)])
            assert rc == 0, out
            return {ln.split()[0]: ln.split() for ln in out.splitlines() if ln.split()}, out

        both = {"git": {"command": "node"}, "fs": {"command": "npx"}}
        got, _ = rows(both)
        assert got["git"][1:] == ["node", "git"], got
        assert got["fs"][1:] == ["npx", "fs"], got

        got, _ = rows(both, {"code": ["git"]})
        assert got["git"][-1] == "code" and got["fs"][-1] == "other", got

        # A name.tool* selector covers the server; the first group in order wins.
        got, _ = rows(both, {"a": ["git.diff*"], "b": ["git"]})
        assert got["git"][-1] == "a" and got["fs"][-1] == "other", got

        # "other" is taken, so the uncovered group is the next name tg_groups picks.
        got, _ = rows({"git": {"command": "node"}}, {"other": ["zzz"]})
        assert got["git"][-1] == "ungrouped", got

        # --json carries the same four fields.
        _write(cfg, {"mcpServers": {"git": {"command": "node", "description": "d"}}})
        rc, out = _capture(["list", "--config", str(cfg), "--json"])
        assert rc == 0, out
        assert json.loads(out) == [{"name": "git", "command": "node", "group": "git",
                                    "description": "d"}], out

        rc, out = _capture(["list", "--config", str(root / "cfg" / "absent.json")])
        assert rc == 1 and "no config found" in out, out


def _check_target_config_order():
    with _hermetic() as root:
        home_cfg = root / ".tool-guardian" / "mcp.json"
        env_cfg = root / "env.json"
        explicit_cfg = root / "explicit.json"
        # Nothing exists and nothing is set -> the home default.
        assert target_config("") == home_cfg, target_config("")
        # $TOOL_GUARDIAN_CONFIG beats the home default when neither exists.
        os.environ["TOOL_GUARDIAN_CONFIG"] = str(env_cfg)
        assert target_config("") == env_cfg, target_config("")
        # An explicit path wins over the env one.
        assert target_config(str(explicit_cfg)) == explicit_cfg, "explicit must win"
        # Once files exist, the FIRST existing path in the search order wins.
        _write(explicit_cfg, {"mcpServers": {}})
        assert target_config(str(explicit_cfg)) == explicit_cfg
        _write(env_cfg, {"mcpServers": {}})
        assert target_config("") == env_cfg, "env comes before mcp.json/.mcp.json/home"
        _write(home_cfg, {"mcpServers": {}})
        assert target_config(str(home_cfg)) == home_cfg, "explicit is first in the order"
        os.environ.pop("TOOL_GUARDIAN_CONFIG", None)
        assert target_config("") == home_cfg, "home default is the last resort"


def _check_group_for_server_matches_tg_groups():
    """group_for_server is only a claim about what tool-guardian WILL do, so hold
    it to the engine that does it: for each server, the group tg_groups._members
    actually files its tools under must be the group list prints."""
    def tools(names):
        return [{"name": n, "description": "", "inputSchema": {}} for n in names]

    cases = [
        (None, ["git", "fs"]),
        ({}, ["git", "fs"]),
        ({"code": ["git"]}, ["git", "fs"]),
        ({"code": ["git", "gh.*"]}, ["git", "gh", "fs"]),
        ({"a": ["git.diff*"], "b": ["git"]}, ["git", "fs"]),
        ({"other": ["zzz"]}, ["git"]),
        ({"other": ["zz"], "ungrouped": ["yy"]}, ["git"]),
        ({"p": ["git.tool1"], "q": ["git.tool2"], "r": ["git"]}, ["git"]),
        ({"w": ["fs.read"], "r": ["fs.write", "fs.stat"]}, ["fs", "git"]),
        ({"z": []}, ["git"]),
        ({"code": ["git.*"]}, ["git", "gh"]),
    ]
    catalogue = ["read", "write", "diff", "tool1", "tool2", "stat"]
    for groups_cfg, names in cases:
        servers = {n: tools(catalogue) for n in names}
        actual = {}
        for group, (members, _unresolved) in tg_groups._members(servers, groups_cfg).items():
            for server, _tool in members:
                actual.setdefault(server, group)
        for name in names:
            mine = group_for_server(name, groups_cfg)
            assert mine == actual.get(name), (
                f"groups={groups_cfg!r} server={name!r}: group_for_server says {mine!r}, "
                f"tg_groups files it under {actual.get(name)!r}")
    # All three fallback names taken: tg_groups numbers the fallback (other_2, ...) and list must agree.
    taken = {"other": ["z"], "ungrouped": ["y"], "ungrouped_tools": ["x"]}
    assert group_for_server("git", taken) == "other_2", group_for_server("git", taken)


CHECKS = [
    ("candidates order + only-existing", _check_candidates),
    ("read_servers bad-JSON message names the file", _check_read_servers_bad_json),
    ("plan_import skips + existing wins + no mutation", _check_plan_import),
    ("write_config backup + atomic content", _check_write_config),
    ("doctor no-config error", _check_doctor_no_config),
    ("doctor missing-command error with fix", _check_doctor_missing_command),
    ("doctor lists-itself error", _check_doctor_lists_itself),
    ("doctor unset ${VAR} warn", _check_doctor_unset_var),
    ("doctor all-good has 0 errors", _check_doctor_all_good),
    ("import --yes writes and prints the snippet", _check_import_yes),
    ("import declined (n) writes nothing", _check_import_no_prompt_writes_nothing),
    ("add_writes_target_and_keeps_other_keys", _check_add_writes_target_and_keeps_other_keys),
    ("add_refuses_duplicate_without_replace", _check_add_refuses_duplicate_without_replace),
    ("add_args_after_double_dash_verbatim", _check_add_args_after_double_dash_verbatim),
    ("add_warns_unset_var_in_env_value", _check_add_warns_unset_var_in_env_value),
    ("add_refuses_tool_guardian_itself", _check_add_refuses_tool_guardian_itself),
    ("remove_unknown_writes_nothing", _check_remove_unknown_writes_nothing),
    ("list_group_default_and_custom_other", _check_list_group_default_and_custom_other),
    ("group_for_server agrees with tg_groups._members", _check_group_for_server_matches_tg_groups),
    ("target_config_prefers_existing_then_env_then_home", _check_target_config_order),
]


def selftest() -> int:
    passed = 0
    for name, fn in CHECKS:
        try:
            fn()  # a check PASSES by finishing without raising
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL %s (%s: %s)" % (name, type(exc).__name__, exc))
        else:
            print("  %s %s" % ("ok  " if ok else "FAIL", name))
        passed += 1 if ok else 0
    total = len(CHECKS)
    print("tg_setup selftest: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if total > 0 and passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
