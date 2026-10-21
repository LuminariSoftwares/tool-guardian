"""Every tool tool-guardian lists over MCP is documented well enough for a model to choose it without a
second guess: what it is for, when a sibling tool is the better call, what it touches, and what comes
back; every parameter has a type, a description and an example, and every optional one says what
happens when it is left out. The README's tool table is checked against the same schemas, and the
router's fixed cost is pinned so the next description edit is a deliberate one (docs/adr/0003)."""
import re
from pathlib import Path

import tool_guardian as tg
from test_tool_annotations import EVERY_TOOL, _tools_over_mcp

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

# `call_tool.args` stays untyped on purpose: models send it as an object or as a JSON string, and the
# router accepts both. A "type" would make a validating client refuse the string form (docs/adr/0004).
UNTYPED = {("call_tool", "args")}
SIBLINGS = EVERY_TOOL | {"activate_group"}
BEHAVIOUR = ("Read-only", "Side effects")


def _tools(tmp_path):
    tools = _tools_over_mcp(tmp_path)
    assert {t["name"] for t in tools} == EVERY_TOOL
    return tools


def test_every_tool_says_purpose_sibling_behaviour_and_output(tmp_path):
    for tool in _tools(tmp_path):
        name, text = tool["name"], tool["description"]
        assert len(text) >= 120, name
        others = [s for s in SIBLINGS - {name} if re.search(r"\b%s\b" % s, text)]
        assert others, "%s names no sibling tool to use instead or next" % name
        assert any(b in text for b in BEHAVIOUR), "%s says nothing about side effects" % name
        assert "Returns" in text, "%s does not say what comes back" % name


def test_read_only_wording_matches_the_annotations(tmp_path):
    for tool in _tools(tmp_path):
        says_read_only = "Read-only" in tool["description"]
        assert says_read_only == tool["annotations"]["readOnlyHint"], tool["name"]


def test_every_parameter_has_type_description_and_example(tmp_path):
    for tool in _tools(tmp_path):
        schema = tool["inputSchema"]
        assert schema.get("type") == "object", tool["name"]
        required = set(schema.get("required", []))
        for pname, spec in schema.get("properties", {}).items():
            where = (tool["name"], pname)
            assert len(spec.get("description", "")) >= 20, where
            assert spec.get("examples") or spec.get("enum"), where  # an enum lists its own values
            if where not in UNTYPED:
                assert spec.get("type") in ("string", "integer", "number", "boolean", "object", "array"), where
            if pname not in required:
                # Optional: either a schema default, or the description says what omitting it does.
                assert "default" in spec or "mit" in spec["description"], where
            if "enum" in spec:
                assert spec.get("default") in spec["enum"], where


def test_defaults_in_the_schema_are_the_ones_the_router_uses():
    tools = {t["name"]: t for t in tg.build_all_tools({})}
    search = tools["search_capabilities"]["inputSchema"]["properties"]
    assert search["limit"]["default"] == 8
    detail = tools["describe_tool"]["inputSchema"]["properties"]["detail"]
    assert detail["default"] == tg.DESCRIBE_DETAIL
    if "retrieve_spill" in tools:
        spill = tools["retrieve_spill"]["inputSchema"]["properties"]
        assert (spill["start_line"]["default"], spill["max_lines"]["default"]) == (1, 400)


def test_readme_tool_table_matches_the_schemas(tmp_path):
    section = README[README.index("## Tools"):]
    section = section[:section.index("\n## ", 1)]
    rows = dict(re.findall(r"^\| `(\w+)` \| ([^|]*) \|", section, re.M))
    for tool in _tools(tmp_path):
        props = tool["inputSchema"].get("properties", {})
        cell = rows[tool["name"]]
        listed = set(re.findall(r"`(\w+)`", cell))
        assert listed == set(props), (tool["name"], listed, set(props))
        if not props:
            assert cell.strip() == "none", tool["name"]


def test_router_cost_is_pinned():
    """Descriptions are paid for on every request. 1,478 is the cost with no backends after the
    2026-10-21 rewrite; raising this number is a decision, write it in the CHANGELOG."""
    assert tg.model_cost(tg.build_all_tools({})) <= 1500


def test_no_backend_description_does_not_contradict_itself():
    """Glama (and any client started before a config exists) inspects the router with no backends.
    The 2026-10-15 score flagged "THESE SERVERS ARE AVAILABLE ...: none reachable" as an unresolved
    template; with nothing live the description now just says so."""
    empty = tg.build_router_tools({})[0]["description"]
    assert "none reachable" not in empty and "THESE SERVERS ARE AVAILABLE" not in empty
    assert "No server is reachable yet" in empty

    class Live:
        status = "ok"
        def hint(self):
            return "git status/diff"
    named = tg.build_router_tools({"git": Live()})[0]["description"]
    assert "THESE SERVERS ARE AVAILABLE" in named and "git (git status/diff)" in named
