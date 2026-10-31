# Per-tool safety policy for the router: deny, ask (confirm token), readOnly.
# Called by: tool_guardian.Router (Router.__init__, catalogue, search, call_tool), tool_guardian.main (--policy-check)
"""Standard library only. MIT licensed.

The policy file is `$TOOL_GUARDIAN_POLICY`, else `~/.tool-guardian/policy.json`:

    {"readOnly": false,
     "servers": {"<server> or *": {"allow": [regex], "deny": [regex], "ask": [regex]}}}

A server's own entry and "*" are merged. Regexes are `re.search`ed on the bare tool name. Order:
readOnly (annotations lack `readOnlyHint: true`) > deny > an `allow` list that nothing in it matches > ask > allow.
No file: every tool is allowed and nothing here is consulted. A file that cannot be read as this shape fails CLOSED:
`Policy.error` is set and every `decide` is a deny.

`ask` is a confirm token, not a human. The token is handed to the MODEL, which can pass it straight back, so it stops
a model that has not understood what it is about to do, not a model that is trying to. A human gate needs the
client's elicitation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

POLICY_ENV = "TOOL_GUARDIAN_POLICY"
TTL_ENV = "TOOL_GUARDIAN_CONFIRM_TTL"
DEFAULT_TTL = 120.0
TOKEN_CHARS = 32            # hex chars of the HMAC kept: 128 bits, short enough for a small model to copy


def default_path() -> str:
    return os.environ.get(POLICY_ENV, "").strip() or str(Path.home() / ".tool-guardian" / "policy.json")


def args_hash(args) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def confirm_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get(TTL_ENV, "") or DEFAULT_TTL))
    except ValueError:
        return DEFAULT_TTL


class Gate:
    """Confirm tokens: HMAC-SHA256 under a per-process secret over server / tool / args-hash / nonce, valid for `ttl`
    seconds from issue, redeemable once. State is in memory, so a restart invalidates every token."""

    def __init__(self, ttl: float = DEFAULT_TTL, clock=time.monotonic, secret: bytes = None):
        self.ttl = ttl
        self.clock = clock
        self._secret = secret if secret is not None else os.urandom(32)
        self._table = {}

    def _mac(self, server: str, tool: str, ahash: str, nonce: str) -> str:
        msg = "\0".join((server, tool, ahash, nonce)).encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:TOKEN_CHARS]

    def _sweep(self, now: float) -> None:
        for tok in [t for t, e in self._table.items() if now - e["issued_at"] >= self.ttl]:
            del self._table[tok]

    def issue(self, server: str, tool: str, ahash: str) -> str:
        now = self.clock()
        self._sweep(now)
        nonce = secrets.token_hex(8)
        token = self._mac(server, tool, ahash, nonce)
        self._table[token] = {"issued_at": now, "nonce": nonce, "server": server, "tool": tool, "args_hash": ahash}
        return token

    def redeem(self, token, server: str, tool: str, ahash: str) -> bool:
        """True once per issued token, for the same server, tool and args, inside the TTL. A token for other args
        or another tool is refused and stays valid for the call it was issued for."""
        entry = self._table.get(token) if isinstance(token, str) else None
        if entry is None:
            return False
        if self.clock() - entry["issued_at"] >= self.ttl:      # a 0 s TTL never redeems
            del self._table[token]
            return False
        want = self._mac(server, tool, ahash, entry["nonce"])
        if not hmac.compare_digest(want, token) or (entry["server"], entry["tool"], entry["args_hash"]) != (
                server, tool, ahash):
            return False
        del self._table[token]
        return True


def _patterns(entry, key: str, where: str) -> list:
    raw = entry.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ValueError("%s.%s must be a list of regex strings" % (where, key))
    out = []
    for p in raw:
        try:
            out.append((p, re.compile(p)))
        except re.error as exc:
            raise ValueError("%s.%s: bad regex %r (%s)" % (where, key, p, exc)) from None
    return out


class Policy:
    """The parsed file. `Policy()` is the empty policy: everything is allowed."""

    def __init__(self, data=None, error: str = "", path: str = ""):
        self.path = path
        self.error = error
        self.read_only = False
        self.rules = {}
        if data is None or error:
            return
        try:
            if not isinstance(data, dict):
                raise ValueError("the top level must be a JSON object")
            self.read_only = bool(data.get("readOnly", False))
            servers = data.get("servers", {})
            if not isinstance(servers, dict):
                raise ValueError("servers must be an object")
            for name, entry in servers.items():
                if not isinstance(entry, dict):
                    raise ValueError("servers.%s must be an object" % name)
                self.rules[name] = {k: _patterns(entry, k, "servers." + name) for k in ("allow", "deny", "ask")}
        except ValueError as exc:
            self.error = str(exc)
            self.read_only, self.rules = False, {}

    @property
    def active(self) -> bool:
        return bool(self.error or self.read_only or self.rules)

    def _merged(self, server: str, key: str) -> list:
        out = []
        for name in dict.fromkeys((server, "*")):
            out += self.rules.get(name, {}).get(key, [])
        return out

    def decide(self, server: str, tool: str, annotations=None) -> tuple:
        """(verdict, rule): verdict is allow / deny / ask. `rule` is what decided it -- `readOnly`, `allow` (an allow
        list that matched nothing), the matching deny or ask regex -- and `""` for a plain allow."""
        if self.error:
            return "deny", "policy file unreadable: " + self.error
        if self.read_only and not (isinstance(annotations, dict) and annotations.get("readOnlyHint") is True):
            return "deny", "readOnly"
        for text, rx in self._merged(server, "deny"):
            if rx.search(tool):
                return "deny", text
        allow = self._merged(server, "allow")
        if allow and not any(rx.search(tool) for _text, rx in allow):
            return "deny", "allow"
        for text, rx in self._merged(server, "ask"):
            if rx.search(tool):
                return "ask", text
        return "allow", ""


def load(path: str = "") -> Policy:
    """Read the policy file. A missing file is the empty policy; one that cannot be read or parsed fails closed."""
    path = path or default_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return Policy(path=path)
    except (OSError, UnicodeDecodeError) as exc:
        return Policy(error=str(exc), path=path)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return Policy(error=str(exc), path=path)
    return Policy(data, path=path)


def exists(path: str = "") -> bool:
    return os.path.isfile(path or default_path())


def count_denials(rows) -> int:
    return sum(1 for r in rows if isinstance(r, dict) and r.get("kind") == "policy" and r.get("decision") == "deny")
