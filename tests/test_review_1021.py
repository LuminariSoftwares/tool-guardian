"""The 2026-10-21 external review, item by item. Every test here failed on 51e0a1e.

1. A config in the current directory ran a cloned repository's MCP servers with no approval.
2. `${VAR}` in a backend's `env` was passed through literally -- the README's own github example.
3. A backend that printed its reason and exited reached the user as BrokenPipeError.
4. An unknown JSON-RPC method answered `{"result": {}}`; a JSON array ended the session.
5. Two bad calls in a minute ended a backend for the rest of the session, however long.
6. The spill archive and the call log were world-readable.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402
from test_tool_guardian import STUB_SERVER  # noqa: E402

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


# ---------------------------------------------------------------------------
# 1. A config in the current directory needs consent
# ---------------------------------------------------------------------------

def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


ONE_SERVER = {"evil": {"command": sys.executable, "args": ["-c", "pass"]}}
SELF_ONLY = {"tool-guardian": {"command": "tool-guardian", "args": []}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A cwd that looks like a cloned repository, and a home config beside it."""
    home = tmp_path / "home"
    _write(home / ".tool-guardian" / "mcp.json", {"mine": {"command": "true"}})
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOOL_GUARDIAN_CONFIG", raising=False)
    monkeypatch.delenv("TOOL_GUARDIAN_PROJECT_CONFIG", raising=False)
    return tmp_path


@pytest.mark.parametrize("name", ["mcp.json", ".mcp.json"])
def test_a_project_config_is_skipped_and_the_skip_is_reported(project, name, capsys):
    _write(project / name, ONE_SERVER)
    assert tg.load_backends() == {"mine": {"command": "true"}}
    err = capsys.readouterr().err
    assert name in err and "NOT loaded" in err
    assert "TOOL_GUARDIAN_PROJECT_CONFIG=1" in err


def test_a_project_config_is_used_when_opted_in(project, monkeypatch):
    _write(project / "mcp.json", ONE_SERVER)
    monkeypatch.setenv("TOOL_GUARDIAN_PROJECT_CONFIG", "1")
    assert set(tg.load_backends()) == {"evil"}


def test_naming_a_project_config_explicitly_is_consent(project, capsys):
    _write(project / "mcp.json", ONE_SERVER)
    assert set(tg.load_backends("mcp.json")) == {"evil"}
    # and it is not ALSO reported as skipped: the log would contradict itself one line later.
    assert "NOT loaded" not in capsys.readouterr().err


def test_a_self_only_project_config_does_not_shadow_the_home_config(project, monkeypatch):
    """The documented Claude Code step copies a .mcp.json whose only server is tool-guardian
    itself. Loading it used to leave the router with zero backends."""
    _write(project / ".mcp.json", SELF_ONLY)
    monkeypatch.setenv("TOOL_GUARDIAN_PROJECT_CONFIG", "1")
    assert tg.load_backends() == {"mine": {"command": "true"}}


def test_the_home_config_is_unaffected(project):
    assert tg.load_backends() == {"mine": {"command": "true"}}


def test_the_config_and_env_actually_loaded_are_logged(tmp_path):
    cfg = tmp_path / "mcp.json"
    _write(cfg, {})
    (tmp_path / ".env").write_text("TG_REVIEW_PROBE=1\n", encoding="utf-8")
    run = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"),
                          "--config", str(cfg), "--selftest"],
                         capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
                         env=dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1"))
    assert str(cfg) in run.stderr, run.stderr
    assert str(tmp_path / ".env") in run.stderr, run.stderr


# ---------------------------------------------------------------------------
# 2. ${VAR} in a backend's env values
# ---------------------------------------------------------------------------

ECHO_ENV = '''
import json, os, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    if msg.get("method") == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "env", "version": "1"}}
    elif msg.get("method") == "tools/list":
        res = {"tools": [{"name": "seen", "description": os.environ.get("BACKEND_TOKEN", "unset"),
                          "inputSchema": {"type": "object", "properties": {}}}]}
    elif mid is None:
        continue
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
    sys.stdout.flush()
'''


def _started(tmp_path, spec):
    backend = tg.Backend("b", spec)
    backend.start()
    return backend


def test_a_var_in_an_env_value_is_expanded(tmp_path, monkeypatch):
    (tmp_path / "srv.py").write_text(ECHO_ENV, encoding="utf-8")
    monkeypatch.setenv("REVIEW_SECRET", "s3cret")
    backend = _started(tmp_path, {"command": sys.executable, "args": [str(tmp_path / "srv.py")],
                                  "env": {"BACKEND_TOKEN": "${REVIEW_SECRET}"}})
    try:
        assert backend.status == "ok", backend.error
        assert backend.tools[0]["description"] == "s3cret"
    finally:
        backend.stop()


def test_an_unset_var_in_an_env_value_stays_literal(tmp_path, monkeypatch):
    """Same rule as `args`: the backend sees the text, so its own error names the variable."""
    (tmp_path / "srv.py").write_text(ECHO_ENV, encoding="utf-8")
    monkeypatch.delenv("REVIEW_ABSENT", raising=False)
    backend = _started(tmp_path, {"command": sys.executable, "args": [str(tmp_path / "srv.py")],
                                  "env": {"BACKEND_TOKEN": "${REVIEW_ABSENT}"}})
    try:
        assert backend.tools[0]["description"] == "${REVIEW_ABSENT}"
    finally:
        backend.stop()


# ---------------------------------------------------------------------------
# 3. A crashed backend keeps its error
# ---------------------------------------------------------------------------

CRASHER = ('import sys\n'
           'sys.stderr.write("FATAL: GITHUB_TOKEN is not set\\n")\n'
           'sys.stderr.flush()\n'
           'raise SystemExit(1)\n')


def test_a_crashed_backend_reports_its_own_error_text(tmp_path):
    (tmp_path / "crash.py").write_text(CRASHER, encoding="utf-8")
    backend = _started(tmp_path, {"command": sys.executable, "args": [str(tmp_path / "crash.py")]})
    assert backend.status in ("UNKNOWN", "DOWN"), backend.status
    assert "GITHUB_TOKEN is not set" in backend.error, backend.error


def test_the_crash_reason_reaches_list_capabilities_and_the_selftest(tmp_path):
    (tmp_path / "crash.py").write_text(CRASHER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    _write(cfg, {"broken": {"command": sys.executable, "args": [str(tmp_path / "crash.py")]}})
    run = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"),
                          "--config", str(cfg), "--selftest"],
                         capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
                         env=dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1"))
    assert "GITHUB_TOKEN is not set" in run.stdout, run.stdout


# ---------------------------------------------------------------------------
# 4. Protocol robustness
# ---------------------------------------------------------------------------

def _speak(tmp_path, lines):
    cfg = tmp_path / "mcp.json"
    _write(cfg, {})
    run = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                         input="".join(lines), capture_output=True, text=True, timeout=120,
                         cwd=str(tmp_path), env=dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1"))
    return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]


HANDSHAKE = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t"}}}) + "\n",
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"]


def test_an_unknown_method_is_method_not_found(tmp_path):
    """A client of the stateless 2026-07-28 revision probes `server/discover`; an empty result
    reads as "supported" and it never falls back."""
    out = _speak(tmp_path, HANDSHAKE + [json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "server/discover"}) + "\n"])
    reply = [m for m in out if m.get("id") == 2][0]
    assert "result" not in reply
    assert reply["error"]["code"] == -32601
    assert "server/discover" in reply["error"]["message"]


def test_ping_is_answered_with_an_empty_result(tmp_path):
    """MCP's keep-alive: a ping must come back as an empty result, with the same id. main
    answered it by accident, through the catch-all that the -32601 above replaced."""
    out = _speak(tmp_path, HANDSHAKE + [json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n"])
    reply = [m for m in out if m.get("id") == 7][0]
    assert reply == {"jsonrpc": "2.0", "id": 7, "result": {}}, reply


@pytest.mark.parametrize("junk", ["[1, 2, 3]", '"a string"', "42"])
def test_a_non_object_line_is_an_invalid_request_and_the_session_survives(tmp_path, junk):
    out = _speak(tmp_path, HANDSHAKE + [junk + "\n", json.dumps(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}) + "\n"])
    bad = [m for m in out if m.get("error", {}).get("code") == -32600]
    assert bad, out
    still_alive = [m for m in out if m.get("id") == 3 and "result" in m]
    assert still_alive, "the router stopped answering after a non-object line"


def test_a_non_ascii_argument_survives_stdin(tmp_path):
    """stdin is read as UTF-8 whatever the console code page is."""
    (tmp_path / "stub.py").write_text(STUB_SERVER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    _write(cfg, {"stub": {"command": sys.executable, "args": [str(tmp_path / "stub.py")]}})
    body = HANDSHAKE + [json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "call_tool",
                                               "arguments": {"server": "stub", "tool": "echo",
                                                             "args": {"text": "naïve café — 日本語"}}}},
                                   ensure_ascii=False) + "\n"]
    run = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                         input="".join(body).encode("utf-8"), capture_output=True, timeout=120,
                         cwd=str(tmp_path), env=dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1"))
    out = [json.loads(line) for line in run.stdout.decode("utf-8").splitlines() if line.strip()]
    text = [m for m in out if m.get("id") == 2][0]["result"]["content"][0]["text"]
    assert "naïve café — 日本語" in text, text


# ---------------------------------------------------------------------------
# 5. A DOWN backend can recover
# ---------------------------------------------------------------------------

FLAKY = '''
import json, os, sys
state = sys.argv[1]
n = 0
try:
    n = int(open(state).read().strip() or "0")
except Exception:
    n = 0
open(state, "w").write(str(n + 1))
if n < 2:
    sys.stderr.write("attempt %d: dependency not ready\\n" % n)
    raise SystemExit(1)
TOOLS = [{"name": "ping", "description": "Return pong.",
          "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    if msg.get("method") == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "flaky", "version": "1"}}
    elif msg.get("method") == "tools/list":
        res = {"tools": TOOLS}
    elif msg.get("method") == "tools/call":
        res = {"content": [{"type": "text", "text": "pong"}]}
    elif mid is None:
        continue
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
    sys.stdout.flush()
'''


def test_a_down_backend_gets_one_more_attempt_after_the_window(tmp_path, monkeypatch):
    """Two failures inside the window still mark it DOWN, with the same status name and the same
    error prefix; what changes is that the window now ends."""
    monkeypatch.setattr(tg, "RESPAWN_WINDOW", 0.2)
    monkeypatch.setattr(tg, "RESPAWN_BACKOFF", (0.0, 0.0))
    (tmp_path / "flaky.py").write_text(FLAKY, encoding="utf-8")
    state = tmp_path / "attempts.txt"
    backend = tg.Backend("flaky", {"command": sys.executable,
                                   "args": [str(tmp_path / "flaky.py"), str(state)]})
    try:
        backend.start()                         # spawn 1 fails
        assert backend.status == "UNKNOWN"
        backend._failed("exited")               # schedules the one normal respawn
        backend._revive()                       # spawn 2 fails -> second failure in the window
        assert backend.status == "DOWN"
        assert "second failure within" in backend.error
        assert "dependency not ready" in backend.error

        backend._revive()          # inside the window: still DOWN, no attempt made
        assert backend.status == "DOWN"

        backend._down_at = tg.time.monotonic() - 1.0   # the window has passed
        backend._revive()
        assert backend.status == "ok", backend.error
        assert backend.tools and backend.tools[0]["name"] == "ping"
    finally:
        backend.stop()


# ---------------------------------------------------------------------------
# 6. The archive and the call log are private
# ---------------------------------------------------------------------------

@POSIX_ONLY
def test_the_spill_archive_is_owner_only(tmp_path):
    import tg_spill
    store = tg_spill.SpillStore(root=tmp_path / "spill")
    saved = store.save("secret output\n" * 10)
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(saved["path"]).st_mode) == 0o600
    sidecar = Path(saved["path"]).with_suffix(".json")
    assert stat.S_IMODE(os.stat(sidecar).st_mode) == 0o600


@POSIX_ONLY
def test_the_call_log_is_owner_only(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "calls.jsonl"
    monkeypatch.setattr(tg, "CALL_LOG", str(log))
    router = tg.Router()
    router.log_call({"kind": "router", "tool": "list_capabilities", "ok": True})
    assert log.is_file()
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
