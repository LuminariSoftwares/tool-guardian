"""The DSH bridge (modules/tg_bridge.py), run the way the plugin runs it: as a
child process, from a working directory that is NOT the repo."""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "modules" / "tg_bridge.py"


def test_selftest_count_line_from_foreign_cwd():
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([sys.executable, str(BRIDGE), "--selftest"], cwd=cwd,
                           capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    m = re.search(r"tg_bridge selftest: (\d+) checks, (\d+) passed, (\d+) failed", r.stdout)
    assert m, r.stdout
    total, passed, failed = (int(x) for x in m.groups())
    assert total >= 7 and passed == total and failed == 0


def test_stdio_roundtrip_is_utf8_and_stdout_is_protocol_only():
    frames = ('{"id": 1, "op": "hello"}\n'
              '{"id": 2, "op": "call", "name": "list_capabilities", "args": {"server": "café"}}\n'
              '{"id": 3, "op": "start", "mcpServers": {"x": {}}}\n'
              '{"id": 4, "op": "call", "name": "list_capabilities", "args": {"server": "café"}}\n'
              '{"id": 5, "op": "shutdown"}\n')
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([sys.executable, str(BRIDGE)], cwd=cwd, input=frames.encode("utf-8"),
                           capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = [json.loads(line) for line in r.stdout.decode("utf-8").splitlines()]
    assert [f["id"] for f in out] == [1, 2, 3, 4, 5]      # every stdout line is a frame
    assert out[0]["ok"] and out[0]["result"]["started"] is False
    assert out[1]["ok"] is False and "call before start" in out[1]["error"]
    # a backend with no `command` is UNKNOWN with a reason -- never an empty list
    assert out[2]["result"]["backends"]["x"]["status"] == "UNKNOWN"
    assert "café" in out[3]["result"]["text"]           # non-ASCII survives both ways
    assert out[4]["result"]["exit"] is True
