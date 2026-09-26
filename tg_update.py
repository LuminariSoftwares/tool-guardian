# @job: tg-update-v1
# @kind: module
"""tg_update.py -- one "update available" line for tool-guardian's --selftest and doctor.  # noqa: E501

Rules this module exists to keep, mirroring the Node notice in update_check.js:
  - never raises and never blocks a front door (callers print exactly one line);
  - never reaches the network more than once a day per install, per machine;
  - never nags a stable user with a prerelease;
  - an opt-out env var kills it outright, and any failure is silent;
  - every network call goes through an injectable door, so tests stay offline.

Standard library only, Python 3.9 - 3.11, ASCII source, LF, no BOM.
MIT licensed, part of the open-source MCP router "tool-guardian".
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

DAY_S = 86400
OPT_OUT_VARS = ("GUARDIAN_NO_UPDATE_CHECK", "NO_UPDATE_NOTIFIER", "CI")
NPM_NAME = "dsh-tool-guardian"
PYPI_NAME = "tool-guardian"

#: Values of an opt-out var that mean "no", not "yes".
OPT_OUT_OFF = frozenset(("0", "false"))

#: The official SemVer 2.0.0 grammar; leading zeroes and loose forms are invalid.
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

#: The PEP 440 spellings PyPI users actually type, folded onto SemVer prerelease tags.
PEP440_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(\d*))?$"
)
PEP440_TAGS = {"a": "alpha", "b": "beta", "rc": "rc"}

NPM_URL = "https://registry.npmjs.org/-/package/{name}/dist-tags"
PYPI_URL = "https://pypi.org/pypi/{name}/json"

OFFLINE_HINT = "could not check (offline?)"
OFFLINE_VARS_HINT = (
    "check off (GUARDIAN_NO_UPDATE_CHECK / NO_UPDATE_NOTIFIER / CI set)"
)


def opted_out(env) -> bool:
    """True when an opt-out var is set to something neither empty nor 0/false.

    Same semantics as the Node notice: a non-string value counts as "yes".
    """
    if not env:
        return False
    for key in OPT_OUT_VARS:
        try:
            raw = env[key]
        except (KeyError, TypeError, IndexError):
            continue
        if raw is None:
            continue
        if not isinstance(raw, str):
            return True
        value = raw.strip()
        if value != "" and value.lower() not in OPT_OUT_OFF:
            return True
    return False


def _is_numeric_identifier(identifier: str) -> bool:
    return identifier.isdigit() and identifier.isascii()


def parse_version(v):
    """Parse SemVer 2.0 and common PEP 440 spellings into a comparable tuple.

    Returns (major, minor, patch, prerelease), where prerelease is None for a
    release or a tuple of SemVer identifier strings. Build metadata is ignored.
    "0.3.0a4" and "0.3.0-alpha.4" both give (0, 3, 0, ("alpha", "4")).
    Anything else is None, so a typo never invents an update.
    """
    if not isinstance(v, str):
        return None
    text = v.strip()
    if not text:
        return None
    match = SEMVER_RE.match(text)
    if match is not None:
        pre = match.group(4)
        ids = tuple(pre.split(".")) if pre is not None else None
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)), ids)
    match = PEP440_RE.match(text)
    if match is not None:
        tag = match.group(4)
        ids = None
        if tag is not None:
            ids = (PEP440_TAGS[tag], match.group(5) or "0")
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)), ids)
    return None


def _compare_prerelease(left, right) -> int:
    """SemVer prerelease precedence: numeric below alphanumeric, then value/ASCII.

    A shorter run of identifiers has lower precedence.
    """
    for index in range(max(len(left), len(right))):
        low = left[index] if index < len(left) else None
        high = right[index] if index < len(right) else None
        if low is None and high is None:
            return 0
        if low is None:
            return -1
        if high is None:
            return 1
        low_numeric = _is_numeric_identifier(low)
        high_numeric = _is_numeric_identifier(high)
        if low_numeric and high_numeric:
            if int(low) != int(high):
                return -1 if int(low) < int(high) else 1
        elif low_numeric != high_numeric:
            return -1 if low_numeric else 1
        elif low != high:
            return -1 if low < high else 1
    return 0


def compare_versions(a, b) -> int:
    """-1/0/1 by SemVer precedence. Either side unparseable compares as 0."""
    left = parse_version(a)
    right = parse_version(b)
    if left is None or right is None:
        return 0
    if left[:3] != right[:3]:
        return -1 if left[:3] < right[:3] else 1
    if left[3] is None and right[3] is None:
        return 0
    if left[3] is None:
        return 1  # a release outranks any of its prereleases
    if right[3] is None:
        return -1
    return _compare_prerelease(left[3], right[3])


def pick_update(current, candidates):
    """The newest candidate strictly greater than current, or None.

    A stable current never gets a prerelease; a prerelease current may be moved
    to anything newer. Unparseable candidates are ignored, and an unparseable
    current never invents an update.
    """
    parsed = parse_version(current)
    if parsed is None:
        return None
    current_is_prerelease = parsed[3] is not None
    best = None
    try:
        values = list(candidates) if candidates else []
    except TypeError:
        return None
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = parse_version(value)
        if candidate is None:
            continue
        if not current_is_prerelease and candidate[3] is not None:
            continue
        if compare_versions(value, current) <= 0:
            continue
        if best is None or compare_versions(value, best) > 0:
            best = value
    return best


def _read_json(path: Path):
    """Parsed JSON from a UTF-8 file, or None for missing/broken/odd content."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _package_name_and_version(root: Path):
    """(name, version) from a package.json beside the module, or (None, None)."""
    data = _read_json(root / "package.json")
    if not isinstance(data, dict):
        return None, None
    name = data.get("name")
    version = data.get("version")
    valid = isinstance(version, str) and version
    return (
        name if isinstance(name, str) else None,
        version if valid else None,
    )


def install_kind(package_root=None, py_version=None) -> dict:
    """Describe this install: the npm plugin, or the PyPI package.

    An npm install ships these .py files next to package.json and shares the
    Node notice's cache file; anything else is a pip install.
    """
    if package_root is None:
        root = Path(__file__).resolve().parent
    else:
        root = Path(package_root)
    name, version = _package_name_and_version(root)
    if name == NPM_NAME:
        return {
            "kind": "npm",
            "name": NPM_NAME,
            "current": version or "0.0.0",
            "update_cmd": f"dsh plugin --profile <name> update {NPM_NAME}",
        }
    return {
        "kind": "pypi",
        "name": PYPI_NAME,
        "current": py_version or _installed_py_version(),
        "update_cmd": f"pip install -U {PYPI_NAME}",
    }


def _installed_py_version() -> str:
    """tool_guardian.__version__, imported lazily and never fatally."""
    try:
        import tool_guardian  # imported here on purpose: it is not a hard dep
    except Exception:  # noqa: BLE001 - any import problem means "unknown"
        return "0.0.0"
    version = getattr(tool_guardian, "__version__", None)
    return version if isinstance(version, str) and version else "0.0.0"


def cache_file(kind, name, home=None) -> Path:
    """Where the once-a-day answer is remembered.

    npm deliberately lands on ~/.cache/dsh-guardians/<name>.update.json: the
    very file update_check.js writes, so both front doors share one answer
    per day.
    """
    base = Path(home) if home is not None else Path.home()
    folder = "dsh-guardians" if kind == "npm" else PYPI_NAME
    return base / ".cache" / folder / (f"{name}.update.json")


def _default_http_get(url, timeout):
    """The real door: one GET, a JSON Accept header, a hard timeout."""
    request = urllib.request.Request(
        url, headers={"accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_candidates(kind, name, http_get=None, timeout=3.0) -> list | None:
    """The versions a registry advertises, or None if it could not be asked.

    npm: the dist-tags document. PyPI: release keys that actually have files.
    """
    get = http_get if http_get is not None else _default_http_get
    try:
        limit = 5.0 if timeout is None else min(float(timeout), 5.0)
        quoted = urllib.parse.quote(str(name), safe="")
        url = NPM_URL.format(name=quoted) if kind == "npm" else PYPI_URL.format(
            name=quoted,
        )
        body = get(url, limit)
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        data = json.loads(body)
    except Exception:  # noqa: BLE001 - offline, 404, HTML, timeout: all silent
        return None
    if not isinstance(data, dict):
        return None
    if kind == "npm":
        return [value for value in data.values() if isinstance(value, str)]
    releases = data.get("releases")
    if not isinstance(releases, dict):
        return None
    return [key for key, files in releases.items() if isinstance(files, list) and files]


def _read_cache(path: Path):
    """(checked_at_ms, next) from the cache file, or None if it is unusable."""
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    checked_at = data.get("checkedAt")
    if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
        return None
    nxt = data.get("next")
    if nxt is not None and not isinstance(nxt, str):
        return None
    return float(checked_at), nxt


def _write_cache(path: Path, checked_at: int, nxt) -> None:
    """Record today's answer.

    Best effort: a cache we cannot write is a cache we simply check again.
    """
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"checkedAt": int(checked_at), "next": nxt}))


def check(info, env=None, now=None, http_get=None, home=None) -> dict:
    """One "update"/"current"/"unknown"/"off" verdict, and it never raises.

    Order matters: opt out, then today's cached answer, then the registry.
    A failed check writes nothing, so an offline day costs one quiet attempt.
    """
    if env is None:
        env = os.environ
    if now is None:
        now = time.time
    kind = info.get("kind")
    name = info.get("name")
    current = info.get("current")
    if not isinstance(current, str) or not current:
        current = "0.0.0"
    result = {"status": "unknown", "current": current, "next": None, "info": info}
    try:
        if opted_out(env):
            result["status"] = "off"
            return result

        path = cache_file(kind, name, home=home)
        try:
            stamp = float(now())
        except Exception:  # noqa: BLE001 - a broken clock is a stale cache
            stamp = 0.0

        cached = _read_cache(path)
        if cached is not None and stamp * 1000.0 - cached[0] < DAY_S * 1000:
            nxt = cached[1]
            newer = isinstance(nxt, str) and compare_versions(nxt, current) > 0
            result["status"] = "update" if newer else "current"
            result["next"] = nxt
            return result

        candidates = fetch_candidates(kind, name, http_get=http_get)
        if candidates is None:
            result["status"] = "unknown"
            return result
        nxt = pick_update(current, candidates)
        _write_cache(path, stamp * 1000, nxt)
        result["status"] = "update" if nxt is not None else "current"
        result["next"] = nxt
        return result
    except Exception:  # noqa: BLE001 - this is a notice, never a failure
        result["status"] = "unknown"
        result["next"] = None
        return result


def status_line(result) -> str:
    """The one line a front door prints. Never empty, never more than one line."""
    info = result.get("info") or {}
    name = info.get("name") or PYPI_NAME
    current = result.get("current")
    status = result.get("status")
    if status == "update":
        return (
            f"update: {name} {result.get('next')} is available "
            f"(you have {current}) -- {info.get('update_cmd', '')}"
        )
    if status == "current":
        return f"update: none -- {current} is the newest"
    hint = OFFLINE_VARS_HINT if status == "off" else OFFLINE_HINT
    return f"update: {hint} -- you have {current}"


# --------------------------------------------------------------------------------------
# selftest -- every check is offline, and every check gets a throwaway home
# --------------------------------------------------------------------------------------

def _fake_http(payloads, calls):
    """A stand-in door: url -> bytes, recording what was asked for."""
    def get(url, timeout):
        calls.append((url, timeout))
        body = payloads.get(url)
        if body is None:
            raise OSError("offline")
        if isinstance(body, bytes):
            return body
        return json.dumps(body).encode("utf-8")
    return get


def _npm_info(current="0.3.0-alpha.4"):
    """The npm install description, matching install_kind()'s npm branch."""
    return {
        "kind": "npm",
        "name": NPM_NAME,
        "current": current,
        "update_cmd": f"dsh plugin --profile <name> update {NPM_NAME}",
    }


def _pypi_info(current="0.3.0"):
    """The pip install description, matching install_kind()'s pypi branch."""
    return {
        "kind": "pypi",
        "name": PYPI_NAME,
        "current": current,
        "update_cmd": f"pip install -U {PYPI_NAME}",
    }


_NPM_PROBE_URL = NPM_URL.format(name=NPM_NAME)
_PYPI_PROBE_URL = PYPI_URL.format(name=PYPI_NAME)
_SELFTEST_NOW = 1790000000.0


def _check_opt_out_never_touches_network(tmp):
    """Rule 1: an opt-out kills the check before any file or door is touched."""
    calls = []
    home = Path(tmp) / "home"
    result = check(
        _pypi_info(),
        env={"GUARDIAN_NO_UPDATE_CHECK": "1"},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http(
            {_PYPI_PROBE_URL: {"releases": {"9.9.9": [{}]}}}, calls,
        ),
        home=home,
    )
    if result["status"] != "off" or calls:
        return f"expected off with no door calls, got {result['status']} / {calls}"
    if cache_file("pypi", PYPI_NAME, home=home).exists():
        return "opted out but still wrote a cache"
    if not opted_out({"CI": "true"}) or opted_out({"CI": " FALSE "}):
        return "opt-out value semantics drifted from the Node notice"
    if opted_out({}) or opted_out({"GUARDIAN_NO_UPDATE_CHECK": ""}):
        return "an absent or empty opt-out var was treated as a yes"
    return None


def _check_fresh_cache_no_network(tmp):
    """Rule 2: today's answer, once recorded, answers without a door call."""
    calls = []
    home = Path(tmp) / "home"
    path = cache_file("npm", NPM_NAME, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = {
        "checkedAt": int((_SELFTEST_NOW - 3600) * 1000),
        "next": "0.3.0-alpha.9",
    }
    path.write_text(json.dumps(fresh), encoding="utf-8")
    result = check(
        _npm_info(),
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http({}, calls),
        home=home,
    )
    if calls:
        return f"a fresh cache still reached the network: {calls}"
    if result["status"] != "update" or result["next"] != "0.3.0-alpha.9":
        return f"the fresh cache was not honoured, got {result}"
    return None


def _check_offline_writes_no_cache(tmp):
    """Rule 3: a check that could not reach the registry leaves no trace."""
    calls = []
    home = Path(tmp) / "home"
    result = check(
        _pypi_info(),
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http({}, calls),
        home=home,
    )
    if result["status"] != "unknown":
        return f"expected unknown, got {result['status']}"
    if cache_file("pypi", PYPI_NAME, home=home).exists():
        return "an offline check wrote a cache"
    if len(calls) != 1:
        return f"an offline check asked the door {len(calls)} times"
    return None


def _check_stable_user_never_offered_prerelease(tmp):
    """Rule 4: a release is not nagged with an alpha, but a prerelease may move."""
    calls = []
    home = Path(tmp) / "home"
    # The registry publishes a stable and a prerelease; the stable user sees one.
    mixed = {_NPM_PROBE_URL: {"latest": "0.3.1", "next": "0.4.0-alpha.1"}}
    result = check(
        _npm_info("0.3.0"),
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http(mixed, calls),
        home=home,
    )
    if pick_update("0.3.0", ["0.4.0-alpha.1", "0.3.1", "0.3.0"]) != "0.3.1":
        return "a stable user was offered a prerelease"
    if result["status"] != "update" or result["next"] != "0.3.1":
        return f"the stable user got {result}, wanted the 0.3.1 release"
    # Only a prerelease is published: the stable user hears nothing, and the
    # day is still recorded so a quiet day stays a quiet day.
    path = cache_file("npm", NPM_NAME, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = {"checkedAt": int((_SELFTEST_NOW - 2 * DAY_S) * 1000), "next": None}
    path.write_text(json.dumps(stale), encoding="utf-8")
    prerelease_only = {_NPM_PROBE_URL: {"next": "0.4.0-alpha.1"}}
    result = check(
        _npm_info("0.3.0"),
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http(prerelease_only, calls),
        home=home,
    )
    if result["status"] != "current" or result["next"] is not None:
        return f"a stable user was nagged with a prerelease: {result}"
    if pick_update("0.4.0-alpha.1", ["0.4.0", "0.4.1-alpha.2"]) != "0.4.1-alpha.2":
        return "a prerelease user could not move forward"
    return None


def _check_pep440_equals_semver_prerelease(tmp):
    """Rule 5: PyPI spellings and SemVer spellings are the same version."""
    home = Path(tmp) / "home"
    if parse_version("0.3.0a4") != parse_version("0.3.0-alpha.4"):
        return "0.3.0a4 does not equal 0.3.0-alpha.4"
    for left, right in (("1.2.3b1", "1.2.3-beta.1"), ("1.2.3rc2", "1.2.3-rc.2")):
        if parse_version(left) != parse_version(right):
            return f"{left} does not equal {right}"
    if compare_versions("0.3.0a4", "0.3.0-alpha.4") != 0:
        return "compare disagrees with parse on PEP 440"
    if parse_version("1.2") is not None or parse_version("junk") is not None:
        return "a loose or junk version parsed"
    if parse_version("1.2.3+build.7") != parse_version("1.2.3"):
        return "build metadata is not ignored"
    # And the live path agrees: PyPI's "0.3.1a1" outranks the SemVer prerelease
    # the user is on, while their own version is never re-offered to them.
    calls = []
    releases = {"releases": {"0.3.0a4": [{}], "0.3.1a1": [{}]}}
    result = check(
        _pypi_info("0.3.0a4"),
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http({_PYPI_PROBE_URL: releases}, calls),
        home=home,
    )
    if result["status"] != "update" or result["next"] != "0.3.1a1":
        return f"a PEP 440 candidate was not picked, got {result}"
    return None


def _check_cache_shape_matches_node_notice(tmp):
    """Rule 6: the cache is the Node notice's file, in the Node notice's shape."""
    home = Path(tmp) / "home"
    path = cache_file("npm", NPM_NAME, home=home)
    shared = home / ".cache" / "dsh-guardians" / f"{NPM_NAME}.update.json"
    if path.resolve() != shared.resolve():
        return f"the npm cache file is not the one update_check.js uses: {path}"
    calls = []
    check(
        _npm_info(),
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http({_NPM_PROBE_URL: {"alpha": "0.3.0-alpha.5"}}, calls),
        home=home,
    )
    data = _read_json(path)
    if not isinstance(data, dict) or set(data) != {"checkedAt", "next"}:
        return f"the cache keys drifted: {data}"
    checked_at = data.get("checkedAt")
    if isinstance(checked_at, bool) or not isinstance(checked_at, int):
        return f"checkedAt is not an int: {checked_at!r}"
    if checked_at != int(_SELFTEST_NOW * 1000):
        return f"checkedAt is not milliseconds: {checked_at!r}"
    if data.get("next") != "0.3.0-alpha.5":
        return f"next is wrong: {data.get('next')!r}"
    # The Node notice is satisfied by a "next" of null on a quiet day, and the
    # file keeps being re-stamped so the check happens once a day, not once a run.
    calls = []
    later = _SELFTEST_NOW + 2 * DAY_S
    check(
        _npm_info("9.9.9"),
        env={},
        now=lambda: later,
        http_get=_fake_http({_NPM_PROBE_URL: {"alpha": "0.3.0-alpha.5"}}, calls),
        home=home,
    )
    quiet = _read_json(path)
    if quiet.get("next") is not None:
        return f"a quiet day did not record next=null: {quiet}"
    if quiet.get("checkedAt") != int(later * 1000):
        return f"a quiet day did not re-stamp checkedAt: {quiet}"
    if len(calls) != 1:
        return f"a stale cache asked the registry {len(calls)} times"
    return None


def _check_default_install_kind_resolves_beside_module(tmp):
    """Rule 7: with no overrides, the kind follows the folder we ship in."""
    home = Path(tmp) / "home"
    info = install_kind()
    here = Path(__file__).resolve().parent
    if (here / "package.json").is_file():
        name, _ = _package_name_and_version(here)
        want = "npm" if name == NPM_NAME else "pypi"
    else:
        want = "pypi"
    if info["kind"] != want:
        return f"the default kind is {info['kind']!r}, expected {want!r}"
    if not isinstance(info["current"], str) or not info["current"]:
        return f"current is not a non-empty string: {info.get('current')!r}"
    if not isinstance(info.get("update_cmd"), str) or not info["update_cmd"]:
        return f"no usable update_cmd: {info.get('update_cmd')!r}"
    # Whatever kind this folder is, an offline check on it asks the door once,
    # says so plainly, and leaves no trace behind.
    calls = []
    result = check(
        info,
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http({}, calls),
        home=home,
    )
    if result["status"] != "unknown" or len(calls) != 1:
        return f"an offline default install misbehaved: {result} / {calls}"
    if cache_file(info["kind"], info["name"], home=home).exists():
        return "an offline check wrote a cache"
    return None


def _check_status_line_single_line(tmp):
    """Rule 8: one line, always prefixed "update: ", and actionable."""
    home = Path(tmp) / "home"
    calls = []
    # End to end: an offline pypi check still has to produce one printable line.
    offline = check(
        _pypi_info(),
        env={},
        now=lambda: _SELFTEST_NOW,
        http_get=_fake_http({}, calls),
        home=home,
    )
    lines = [
        status_line({
            "status": "update", "current": "0.3.0",
            "next": "0.3.1", "info": _pypi_info(),
        }),
        status_line({
            "status": "current", "current": "0.3.0",
            "next": None, "info": _pypi_info(),
        }),
        status_line(offline),
        status_line({
            "status": "off", "current": "0.3.0",
            "next": None, "info": _pypi_info(),
        }),
    ]
    for line in lines:
        if not isinstance(line, str) or not line.startswith("update: "):
            return f"not an update line: {line!r}"
        if "\n" in line or "\r" in line:
            return f"not a single line: {line!r}"
    if "0.3.1" not in lines[0] or "pip install -U tool-guardian" not in lines[0]:
        return f"the update line is not actionable: {lines[0]!r}"
    if "0.3.0" not in lines[1] or "GUARDIAN_NO_UPDATE_CHECK" not in lines[3]:
        return f"a line is missing its details: {lines}"
    if "offline" not in lines[2]:
        return f"the offline line does not say so: {lines[2]!r}"
    return None


SELFTEST_CHECKS = (
    ("opt_out_never_touches_network", _check_opt_out_never_touches_network),
    ("fresh_cache_no_network", _check_fresh_cache_no_network),
    ("offline_writes_no_cache", _check_offline_writes_no_cache),
    ("stable_user_never_offered_prerelease",
     _check_stable_user_never_offered_prerelease),
    ("pep440_equals_semver_prerelease", _check_pep440_equals_semver_prerelease),
    ("cache_shape_matches_node_notice", _check_cache_shape_matches_node_notice),
    ("default_install_kind_resolves_beside_module",
     _check_default_install_kind_resolves_beside_module),
    ("status_line_single_line", _check_status_line_single_line),
)


def selftest(stream=None) -> int:
    """Run every named check, print one line each, and return an exit code."""
    out = stream if stream is not None else sys.stdout
    passed = 0
    failed = 0
    for name, check_fn in SELFTEST_CHECKS:
        with tempfile.TemporaryDirectory(prefix="tg_update_selftest_") as tmp:
            try:
                why = check_fn(tmp)
            except Exception as exc:  # noqa: BLE001 - a broken check failed
                why = f"{type(exc).__name__}: {exc}"
        if why:
            failed += 1
            out.write(f"FAIL {name} ({why})\n")
        else:
            passed += 1
            out.write(f"ok   {name}\n")
    total = len(SELFTEST_CHECKS)
    out.write(
        f"tg_update selftest: {total} checks, {passed} passed, {failed} failed\n",
    )
    out.flush()
    return 0 if total > 0 and failed == 0 else 1


def main(argv=None) -> int:
    """--selftest runs the offline checks; no args prints the one update line."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--selftest", "-s", "selftest"):
        return selftest()
    if args and args[0] in ("-h", "--help"):
        sys.stdout.write("usage: tg_update.py [--selftest]\n")
        return 0
    try:
        sys.stdout.write(f"{status_line(check(install_kind()))}\n")
    except Exception:  # noqa: BLE001 - even this line must never fail
        sys.stdout.write(f"update: {OFFLINE_HINT} -- you have an unknown version\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
