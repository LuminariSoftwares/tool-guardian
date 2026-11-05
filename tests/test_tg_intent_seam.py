"""P141 S2b: `intent` on call_tool -- keep only the matching chunks of a big result (the full original is archived).

Opt-in (TOOL_GUARDIAN_INTENT=1) until the P110 pins (exact call_tool properties, tools/list budget) are ruled on;
off, the router's bytes are today's (tests/test_ladder_identity.py). The idea comes from context-mode; rewritten
from the idea."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARAS = ["Paragraph %d is routine filler about weather number %d." % (i, i) for i in range(500)]
PARAS[250] = "Paragraph 250 says the csv exporter writes rows with a header line."
DOC = "\n\n".join(PARAS)
FS = r'''
import json, sys
DOC = open(sys.argv[1], encoding="utf-8").read()
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
               "serverInfo": {"name": "docs", "version": "1"}}
    elif msg["method"] == "tools/list":
        res = {"tools": [{"name": "fetch_doc", "description": "Fetch.", "inputSchema": {"type": "object"}}]}
    else:
        res = {"content": [{"type": "text", "text": DOC}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
'''


def run(tmp_path, on, intent):
    (tmp_path / "fs.py").write_text(FS, encoding="utf-8")
    (tmp_path / "doc.txt").write_text(DOC, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"docs": {"command": sys.executable,
                                                       "args": [str(tmp_path / "fs.py"), str(tmp_path / "doc.txt")]}}}),
                   encoding="utf-8")
    env = dict(os.environ, GUARDIAN_NO_UPDATE_CHECK="1", PYTHONIOENCODING="utf-8",
               TOOL_GUARDIAN_SPILL_DIR=str(tmp_path / "spill"), TOOL_GUARDIAN_STATE=str(tmp_path / "state.json"),
               TOOL_GUARDIAN_POLICY=str(tmp_path / "none.json"))
    for k in ("TOOL_GUARDIAN_TERSE", "TOOL_GUARDIAN_PRESET", "TOOL_GUARDIAN_LADDER", "TOOL_GUARDIAN_INTENT"):
        env.pop(k, None)
    if on:
        env["TOOL_GUARDIAN_INTENT"] = "1"
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "call_tool", "arguments": {"server": "docs", "tool": "fetch_doc", "args": {},
                                                           "intent": intent}}}]
    out = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                         input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True,
                         encoding="utf-8", timeout=120, env=env, cwd=str(tmp_path))
    got = {json.loads(x)["id"]: json.loads(x)["result"] for x in out.stdout.splitlines() if x.strip()}
    props = next(t for t in got[2]["tools"] if t["name"] == "call_tool")["inputSchema"]["properties"]
    return props, "".join(c.get("text", "") for c in got[3]["content"])


def test_off_by_default_schema_and_bytes(tmp_path):
    props, text = run(tmp_path, False, "csv exporter")
    assert "intent" not in props
    assert "[chunk " not in text and "searchable terms:" not in text


def test_on_keeps_the_matching_chunk_and_points_at_the_archive(tmp_path):
    props, text = run(tmp_path, True, "csv exporter")
    assert props["intent"]["maxLength"] == 200
    assert "Paragraph 250 says the csv exporter" in text and "[chunk " in text
    assert "searchable terms:" in text and re.search(r"sp_[0-9a-f]{12}", text)
    assert "Paragraph 3 is" not in text


def test_on_an_overlong_intent_is_ignored(tmp_path):
    _props, text = run(tmp_path, True, "x" * 201)
    assert "[chunk " not in text
