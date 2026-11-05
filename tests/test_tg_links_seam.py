"""P141 S3b: needs-X hints under search_capabilities hits (opt-in, TOOL_GUARDIAN_HINTS=1).

Measured before the splice (bench/bench_links.py, P141 S3): producer recall baseline 20.8% -> links 37.5%. Off by
default so tests/test_rank_identity.py's frozen search() output stays byte-identical. Rewritten from the idea."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = [{"name": "list_issues", "description": "List issues.", "inputSchema": {"type": "object"},
          "outputSchema": {"type": "object", "properties": {"issueId": {"type": "string"}}}},
         {"name": "get_issue", "description": "Get one issue by id.",
          "inputSchema": {"type": "object", "required": ["issue_id"], "properties": {"issue_id": {"type": "string"}}}}]
FS = r'''
import json, sys
TOOLS = json.loads(sys.argv[1])
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "gh", "version": "1"}}
    elif msg["method"] == "tools/list":
        res = {"tools": TOOLS}
    else:
        res = {"content": [{"type": "text", "text": "ok"}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
'''


def search(tmp_path, hints):
    (tmp_path / "fs.py").write_text(FS, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"gh": {"command": sys.executable,
                                                     "args": [str(tmp_path / "fs.py"), json.dumps(TOOLS)]}}}),
                   encoding="utf-8")
    env = dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1", PYTHONIOENCODING="utf-8", TOOL_GUARDIAN_EMBED_URL="",
               TOOL_GUARDIAN_STATE=str(tmp_path / "s.json"), TOOL_GUARDIAN_POLICY=str(tmp_path / "none.json"))
    env.pop("TOOL_GUARDIAN_HINTS", None)
    if hints:
        env["TOOL_GUARDIAN_HINTS"] = "1"
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "search_capabilities", "arguments": {"query": "get issue"}}}]
    out = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                         input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True,
                         encoding="utf-8", timeout=120, env=env, cwd=str(tmp_path))
    msg = next(json.loads(x) for x in out.stdout.splitlines() if x.strip() and json.loads(x).get("id") == 2)
    return "".join(c.get("text", "") for c in msg["result"]["content"])


def test_hint_shows_when_on(tmp_path):
    assert "needs issue_id from: gh.list_issues" in search(tmp_path, True)


def test_off_by_default(tmp_path):
    assert "needs " not in search(tmp_path, False)
