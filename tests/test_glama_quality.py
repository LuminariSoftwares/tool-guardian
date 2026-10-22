"""Glama scores each MCP tool on how its description and parameters read. Every parameter is documented, every
description says what the tool returns, and the README names each tool once and says which install is which."""
import json
import re
import subprocess
import sys
from pathlib import Path

from test_tool_annotations import EVERY_TOOL, _tools_over_mcp

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
README_TOOLS = ("list_capabilities", "search_capabilities", "describe_tool", "call_tool",
                "list_groups_with_costs", "retrieve_spill", "list_skills", "read_skill")


def test_every_parameter_is_documented(tmp_path):
    """Explained once, and in the cheapest place that works: a parameter carries its own
    description unless its `enum` already lists the values or the tool description names it.
    tests/test_tool_definitions.py holds the same rule with the reasoning."""
    tools = _tools_over_mcp(tmp_path)
    assert {t["name"] for t in tools} == EVERY_TOOL
    for tool in tools:
        for pname, spec in tool["inputSchema"].get("properties", {}).items():
            where = (tool["name"], pname)
            assert (len(spec.get("description", "")) >= 20 or "enum" in spec
                    or re.search(r"\b%s\b" % pname, tool["description"])), where


def test_every_description_says_what_comes_back(tmp_path):
    for tool in _tools_over_mcp(tmp_path):
        assert "Returns" in tool["description"] or "returns" in tool["description"], tool["name"]


def test_readme_names_each_tool_once_in_the_tools_table():
    section = README[README.index("## Tools"):]
    section = section[:section.index("\n## ", 1)]
    for name in README_TOOLS:
        assert len(re.findall(r"^\| `%s`" % name, section, re.M)) == 1, name


def test_readme_tool_counts_match_the_code():
    assert "four small generic tools" not in README
    assert "6 router tools (8 with [skills](#skills))" in README


def test_readme_says_which_install_is_which():
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    py = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert "Python package `tool-guardian` **%s**" % py in README
    assert "npm package `dsh-tool-guardian` **%s**" % pkg in README


def test_readme_has_a_claude_desktop_uvx_config():
    block = re.search(r"```json\n(\{\s*\"mcpServers\": \{\s*\"tool-guardian\": \{\s*\"command\": \"uvx\".*?)```", README, re.S).group(1)
    server = json.loads(block)["mcpServers"]["tool-guardian"]
    assert server["args"][0] == "tool-guardian" and "--config" in server["args"]
    assert "mcp-name: io.github.LuminariSoftwares/tool-guardian" in README.splitlines()


def test_glama_json_is_valid_and_names_a_maintainer():
    meta = json.loads((ROOT / "glama.json").read_text(encoding="utf-8"))
    assert meta["$schema"] == "https://glama.ai/mcp/schemas/server.json"
    assert meta["maintainers"] and all(isinstance(m, str) and m for m in meta["maintainers"])


def test_dockerfile_copies_every_module_the_package_installs():
    """The image installs the package from the copied files; a module added to pyproject.toml but
    not to the COPY lines would build and then fail on import."""
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    modules = re.findall(r'"(\w+)"', re.search(r"py-modules = \[([^\]]*)\]", pyproject).group(1))
    copied = " ".join(l for l in docker.splitlines() if l.startswith("COPY "))
    for m in modules:
        assert (m + ".py") in copied or (m.startswith("tg_") and "tg_*.py" in copied), m
    for f in ("pyproject.toml", "README.md", "LICENSE"):
        assert f in copied, f
    assert 'ENTRYPOINT ["tool-guardian"]' in docker


def test_readme_headline_figures_come_from_the_bench():
    """Every token figure in the README's headline, hero table and savings section is what
    `python bench/bench_tokens.py --json` prints today -- not a number typed once and left
    behind. (On 2026-10-21 the README said 1,790 while the bench said something else.)"""
    out = subprocess.run([sys.executable, str(ROOT / "bench" / "bench_tokens.py"), "--json"],
                         capture_output=True, text=True, timeout=300, check=True).stdout
    bench = json.loads(out)["catalog"]
    full = "{:,}".format(bench["full_tokens"])
    router = "{:,}".format(bench["router_tokens"])
    pct = "%.1f" % bench["saved_pct"]

    assert ("<b>Seven MCP servers for %s tokens instead of %s" % (router, full)) in README
    assert ("**%s tokens** of schemas on every request" % full) in README
    assert ("**%s tokens** (3 %%); a schema is fetched only when the model asks" % router) in README
    assert ("(%s tokens with seven servers behind it, measured below)" % router) in README
    assert ("**Every request:** %s tokens of tool schemas without tool-guardian, %s with it "
            "(**%s %% less**)" % (full, router, pct)) in README
    for row in bench["servers"]:
        assert "| %s | %d | {:,} |".format(row["tokens"]) % (row["server"], row["tools"]) in README
    assert "| **all seven** | **%d** | **%s** |" % (bench["total_tools"], full) in README
