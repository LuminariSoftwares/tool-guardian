"""Run each real MCP client against the configs in examples/ and prove it drives tool-guardian.

    pip install -e .                 # puts `tool-guardian` on PATH, as a user's install does
    python tests/clients/run_clients.py                     # every client found on PATH
    python tests/clients/run_clients.py --only opencode --bin cline=/path/to/cline

For each client it starts tests/clients/mock_model.py (a scripted model, no API key, no
network), a stub MCP backend behind tool-guardian, and an isolated HOME, hands the client the
example's tool-guardian entry from examples/ unchanged (Claude Code through `claude mcp add-json`;
OpenCode and Continue with the mock model provider added beside it; Cline the file as is), asks
one question and checks three things:

  1. the client sent tool-guardian's router tools to the model;
  2. the model's `list_capabilities` call reached the router and its answer (the stub
     backend's catalogue) came back to the model;
  3. tool-guardian's call log has the client's session row and an ok `list_capabilities` row.

Dev harness, not part of the CI matrix (it needs the client CLIs installed). Standard library only.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def _stub_server_source():
    """The stub MCP backend the unit tests use, read without importing pytest."""
    import ast
    tree = ast.parse((ROOT / "tests" / "test_tool_guardian.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "STUB_SERVER" for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("STUB_SERVER not found in tests/test_tool_guardian.py")


STUB_SERVER = _stub_server_source()

QUESTION = "Which servers are behind the router?"
CLIENTS = ("claude-code", "opencode", "cline", "continue")
DEFAULT_BINS = {"claude-code": "claude", "opencode": "opencode", "cline": "cline", "continue": "cn"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rows(path):
    try:
        return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def _run(cmd, env, cwd, timeout=240):
    """Run a client, killing its whole process group on timeout (Cline keeps a worker alive)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    env = dict(env, PWD=str(cwd))      # OpenCode finds its project config from $PWD, not the real cwd
    proc = subprocess.Popen(cmd, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return "timeout", out.decode("utf-8", "replace")


class Case:
    def __init__(self, name, binary, work, base_url, mock_log):
        self.name, self.bin, self.base_url, self.mock_log = name, binary, base_url, mock_log
        self.dir = work / name
        self.home = self.dir / "home"
        self.proj = self.dir / "project"
        self.home.mkdir(parents=True)
        self.proj.mkdir(parents=True)
        stub = self.dir / "stub_server.py"
        stub.write_text(STUB_SERVER, encoding="utf-8")
        self.backends = self.dir / "mcp.json"
        self.backends.write_text(json.dumps({"mcpServers": {"stub": {
            "command": sys.executable, "args": [str(stub)], "description": "stub echo server"}}}), encoding="utf-8")
        self.calls = self.dir / "calls.jsonl"

    def env(self, **extra):
        keep = {k: v for k, v in os.environ.items()
                if not k.startswith(("CLAUDE", "ANTHROPIC", "OPENAI", "TOOL_GUARDIAN", "OPENCODE", "CLINE",
                                     "GH_", "GITHUB_", "COPILOT"))}
        keep.update(HOME=str(self.home), USERPROFILE=str(self.home),
                    XDG_CONFIG_HOME=str(self.home / ".config"), XDG_DATA_HOME=str(self.home / ".local" / "share"),
                    XDG_STATE_HOME=str(self.home / ".local" / "state"), XDG_CACHE_HOME=str(self.home / ".cache"),
                    TOOL_GUARDIAN_CONFIG=str(self.backends), TOOL_GUARDIAN_CALL_LOG=str(self.calls),
                    TOOL_GUARDIAN_STATE=str(self.dir / "state.json"), GUARDIAN_NO_UPDATE_CHECK="1",
                    NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        keep.update(extra)
        return keep

    def version(self):
        try:
            r = subprocess.run([self.bin, "--version"], capture_output=True, text=True, timeout=60,
                               env=self.env(), stdin=subprocess.DEVNULL)
            return (r.stdout.strip() or r.stderr.strip()).splitlines()[-1]
        except Exception as exc:  # noqa: BLE001
            return "? (%s)" % exc


# ---- one setup per client: the example file goes in unchanged -----------------------------

def run_claude_code(case):
    server = json.loads((EXAMPLES / "claude-code" / "project.mcp.json").read_text(encoding="utf-8"))
    entry = json.dumps(server["mcpServers"]["tool-guardian"])
    env = case.env(ANTHROPIC_BASE_URL=case.base_url, ANTHROPIC_API_KEY="sk-ant-mock", DISABLE_TELEMETRY="1",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    code, out = _run([case.bin, "mcp", "add-json", "--scope", "user", "tool-guardian", entry], env, case.proj, 60)
    if code != 0:
        return code, out
    return _run([case.bin, "-p", QUESTION, "--allowedTools", "mcp__tool-guardian__list_capabilities"],
                env, case.proj)


def run_opencode(case):
    config = json.loads((EXAMPLES / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    config["provider"] = {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "mock",
                                   "options": {"baseURL": case.base_url + "/v1"},
                                   "models": {"mock-router-model": {}}}}
    config["model"] = "mock/mock-router-model"
    (case.proj / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    # `mcp list` connects every MCP server: a check of its own before the model turn.
    code, out = _run([case.bin, "mcp", "list"], case.env(), case.proj, 120)
    if code != 0 or "connected" not in out:
        return code or "mcp list: not connected", out
    cmd = [case.bin, "run", "--print-logs", "-m", "mock/mock-router-model", QUESTION]
    return _run(cmd, case.env(), case.proj)


def run_cline(case):
    settings = case.home / ".cline" / "data" / "settings"
    settings.mkdir(parents=True)
    shutil.copyfile(EXAMPLES / "cline" / "cline_mcp_settings.json", settings / "cline_mcp_settings.json")
    env = case.env()
    code, out = _run([case.bin, "auth", "-p", "openai", "-k", "mock", "-m", "mock-router-model",
                      "-b", case.base_url + "/v1"], env, case.proj, 60)
    if code != 0:
        return code, out
    return _run([case.bin, "-c", str(case.proj), "-y", QUESTION], env, case.proj)


def run_continue(case):
    block = (EXAMPLES / "continue" / "tool-guardian.yaml").read_text(encoding="utf-8")
    servers = block[block.index("mcpServers:"):]         # the example's own mcpServers section, verbatim
    config = case.proj / "config.yaml"
    config.write_text("name: tool-guardian-client-test\nversion: 0.0.1\nschema: v1\n"
                      "models:\n  - name: mock\n    provider: openai\n    model: mock-router-model\n"
                      "    apiBase: %s/v1\n    apiKey: mock\n    roles: [chat]\n" % case.base_url + servers,
                      encoding="utf-8")
    return _run([case.bin, "--config", str(config), "--auto", "-p", QUESTION], case.env(), case.proj)


RUNNERS = {"claude-code": run_claude_code, "opencode": run_opencode, "cline": run_cline, "continue": run_continue}


def check(case, mock_rows):
    calls = _rows(case.calls)
    sent = any(any("list_capabilities" in t for t in row["tools"]) for row in mock_rows)
    answered = any(any("[stub] 2 tools" in r for r in row["tool_results"]) for row in mock_rows)
    session = [r.get("client") for r in calls if r.get("kind") == "session"]
    routed = any(r.get("kind") == "router" and r.get("tool") == "list_capabilities" and r.get("ok") for r in calls)
    problems = []
    if not sent:
        problems.append("router tools never reached the model")
    if not answered:
        problems.append("the router's answer never came back to the model")
    if not session:
        problems.append("no session row in the call log")
    if not routed:
        problems.append("no ok list_capabilities row in the call log")
    return problems, (session[0] if session else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description="run real MCP clients against examples/")
    ap.add_argument("--only", action="append", choices=CLIENTS, help="run just this client (repeatable)")
    ap.add_argument("--bin", action="append", default=[], metavar="CLIENT=PATH", help="client binary override")
    ap.add_argument("--keep", action="store_true", help="keep the work folder and print its path")
    a = ap.parse_args(argv)

    if shutil.which("tool-guardian") is None:
        print("tool-guardian is not on PATH: run `pip install -e .` first (the examples call it by name)")
        return 2
    bins = dict(DEFAULT_BINS)
    for item in a.bin:
        name, _, path = item.partition("=")
        bins[name] = path
    work = Path(tempfile.mkdtemp(prefix="tg-clients-"))
    port = _free_port()
    base_url = "http://127.0.0.1:%d" % port
    mock_log = work / "mock_requests.jsonl"
    mock = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mock_model.py")),
                             "--port", str(port), "--log", str(mock_log)], stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    results = []
    try:
        for name in a.only or CLIENTS:
            binary = shutil.which(bins[name]) or (bins[name] if Path(bins[name]).exists() else None)
            if not binary:
                results.append((name, "-", "SKIP", "not installed (%s)" % bins[name]))
                continue
            case = Case(name, binary, work, base_url, mock_log)
            version = case.version()
            mock_log.write_text("", encoding="utf-8")
            code, out = RUNNERS[name](case)
            problems, client_id = check(case, _rows(mock_log))
            (case.dir / "client_output.txt").write_text(out, encoding="utf-8")
            if code != 0:
                problems.insert(0, "exit %s" % code)
            verdict = "PASS" if not problems else "FAIL"
            detail = ("MCP clientInfo: %s" % client_id) if not problems else "; ".join(problems)
            results.append((name, version, verdict, detail))
    finally:
        mock.terminate()
        mock.wait(timeout=10)

    print("tool-guardian client check (%s)" % sys.platform)
    for name, version, verdict, detail in results:
        print("  %-12s %-28s %s  %s" % (name, version[:28], verdict, detail))
    if a.keep:
        print("work folder: %s" % work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    failed = [r for r in results if r[2] == "FAIL"]
    print("%d passed, %d failed, %d skipped" % (sum(r[2] == "PASS" for r in results), len(failed),
                                                 sum(r[2] == "SKIP" for r in results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
