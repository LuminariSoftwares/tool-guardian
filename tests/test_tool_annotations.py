"""MCP tool annotations (2026-10-15): every tool tool-guardian exposes carries a title and the
four hints, as booleans, over the real MCP protocol. And the official MCP Registry markers.
Each test failed on main before the change."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402
from test_tool_guardian import STUB_SERVER  # noqa: E402

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
EVERY_TOOL = {"list_capabilities", "describe_tool", "call_tool", "search_capabilities",
              "list_groups_with_costs", "retrieve_spill", "list_skills", "read_skill"}


def _tools_over_mcp(tmp_path):
    """Start tool-guardian as a stdio MCP server (one stub backend, one skill configured, so
    every optional tool is present), send initialize + tools/list, return the tools."""
    (tmp_path / "stub.py").write_text(STUB_SERVER, encoding="utf-8")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"stub": {"command": sys.executable,
                                                       "args": [str(tmp_path / "stub.py")]}}}), encoding="utf-8")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: a demo skill\n---\nbody\n", encoding="utf-8")
    env = dict(__import__("os").environ, TOOL_GUARDIAN_SKILLS=str(tmp_path / "skills"))
    env.pop("TOOL_GUARDIAN_LADDER", None)
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    r = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--config", str(cfg)],
                       input="".join(json.dumps(m) + "\n" for m in requests), capture_output=True,
                       text=True, env=env, timeout=60, cwd=str(tmp_path))
    replies = {m["id"]: m for m in map(json.loads, r.stdout.splitlines())}
    return replies[2]["result"]["tools"]


def test_every_tool_listed_over_mcp_carries_a_title_and_four_boolean_hints(tmp_path):
    tools = _tools_over_mcp(tmp_path)
    assert {t["name"] for t in tools} == EVERY_TOOL
    for t in tools:
        ann = t.get("annotations")
        assert isinstance(ann, dict), t["name"]
        for hint in HINTS:
            assert isinstance(ann.get(hint), bool), (t["name"], hint)
        assert isinstance(t.get("title"), str) and t["title"].strip(), t["name"]
        assert ann.get("title") == t["title"], t["name"]


def test_read_only_tools_say_so_and_call_tool_is_the_conservative_one(tmp_path):
    ann = {t["name"]: t["annotations"] for t in _tools_over_mcp(tmp_path)}
    for name in EVERY_TOOL - {"call_tool"}:
        assert ann[name]["readOnlyHint"] is True and ann[name]["destructiveHint"] is False, name
        assert ann[name]["openWorldHint"] is False, name
    assert ann["call_tool"] == dict(ann["call_tool"], readOnlyHint=False, destructiveHint=True,
                                    idempotentHint=False, openWorldHint=True)


def test_annotations_do_not_count_as_model_tokens():
    tools = tg.build_all_tools({})
    bare = [{k: v for k, v in t.items() if k not in ("title", "annotations")} for t in tools]
    assert tg.model_cost(tools) == tg.est_tokens(bare)


# ---- MCP Registry markers ----------------------------------------------------------

REGISTRY_NAME = "io.github.luminarisoftwares/tool-guardian"


def test_registry_markers_exist_and_agree():
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = re.findall(r"(?m)^mcp-name: (\S+)$", readme)
    assert pkg.get("mcpName") == REGISTRY_NAME
    assert lines == [REGISTRY_NAME]
    assert readme.index("mcp-name: ") < readme.index("## ")      # near the top, before the first section
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'(?m)^readme = "README\.md"$', pyproject)    # the README PyPI receives
    assert "README.md" in pkg["files"]                              # and the one npm ships
