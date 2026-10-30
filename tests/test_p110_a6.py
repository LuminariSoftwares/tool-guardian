"""P110 S4 (A6) acceptance -- llms.txt and a one-prompt agent install. Written by the O5 overseer; DO NOT EDIT.

Why: a coding agent told "install tool-guardian" today has to read a 770-line README. The llms.txt convention
(llmstxt.org) gives it one short, link-only map, and a README line a user can paste to their agent does the rest.
Idea taken from XERJ's install line; no code from it.

Contract (plan P110 rev 1, decision 9 + C7):
- `llms.txt` at the repo root, UTF-8, under 6,000 characters. First line `# tool-guardian`; then a `> ` blockquote
  summary line; then H2 sections in this order: `## Install`, `## Configure`, `## Tools`, `## Limits`.
- Every markdown link in it is either a relative path to a file that exists in the repo, or a
  https://github.com/LuminariSoftwares/tool-guardian URL (`/blob/main/<path>` to an existing file, optionally with
  `#anchor` = a real README heading slug). No other hosts.
- `## Tools` names all six router tools; `## Limits` links to the README "What it does *not* do (yet)" heading.
- The README carries the one-prompt install line: a line containing the raw llms.txt URL
  https://raw.githubusercontent.com/LuminariSoftwares/tool-guardian/main/llms.txt , OUTSIDE the
  `<!-- guardian-family:start -->` / `<!-- guardian-family:end -->` block (that block stays byte-identical across
  the four repos; tests/test_family_block.py guards it).
- llms.txt ships in the npm package (`package.json` "files") so `npx` users have it too.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLMS = ROOT / "llms.txt"
RAW = "https://raw.githubusercontent.com/LuminariSoftwares/tool-guardian/main/llms.txt"
REPO = "https://github.com/LuminariSoftwares/tool-guardian"
SIX = ("list_capabilities", "search_capabilities", "describe_tool", "call_tool",
       "list_groups_with_costs", "retrieve_spill")


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, drop punctuation except '-' and ' ', spaces to '-'."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def readme_slugs():
    return {slug(line.lstrip("#")) for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("#")}


def text():
    assert LLMS.is_file(), "llms.txt is missing at the repo root"
    return LLMS.read_text(encoding="utf-8")


def test_structure():
    t = text()
    lines = t.splitlines()
    assert lines[0].strip() == "# tool-guardian"
    assert any(line.startswith("> ") for line in lines[1:6]), "a '> ' summary line right after the title"
    heads = [line.strip() for line in lines if line.startswith("## ")]
    want = ["## Install", "## Configure", "## Tools", "## Limits"]
    assert [h for h in heads if h in want] == want, heads
    assert len(t) < 6000, len(t)


def test_every_link_resolves():
    links = re.findall(r"\]\(([^)\s]+)\)", text())
    assert links, "llms.txt has no links"
    slugs = readme_slugs()
    for link in links:
        if link.startswith("http"):
            assert link.startswith(REPO + "/") or link == REPO, "foreign host: " + link
            path, _, anchor = link[len(REPO):].lstrip("/").partition("#")
            if path.startswith("blob/main/"):
                rel = path[len("blob/main/"):]
                assert (ROOT / rel).exists(), "no such file: " + rel
                if anchor:
                    assert rel == "README.md" and anchor in slugs, "no such README heading: #" + anchor
            else:
                assert path in ("",) and (not anchor or anchor in slugs), link
        else:
            rel, _, anchor = link.partition("#")
            assert (ROOT / rel).exists(), "no such file: " + rel
            if anchor:
                assert rel == "README.md" and anchor in slugs, "no such README heading: #" + anchor


def test_tools_and_limits_sections():
    t = text()
    tools = t.split("## Tools", 1)[1].split("\n## ", 1)[0]
    for name in SIX:
        assert name in tools, name
    limits = t.split("## Limits", 1)[1].split("\n## ", 1)[0]
    assert "#" + slug("What it does *not* do (yet)") in limits


def test_readme_has_the_install_line_outside_the_family_block():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start, end = readme.index("<!-- guardian-family:start -->"), readme.index("<!-- guardian-family:end -->")
    hits = [m.start() for m in re.finditer(re.escape(RAW), readme)]
    assert hits, "README needs the one-prompt install line with " + RAW
    assert any(not (start < h < end) for h in hits), "the install line must sit outside the family block"


def test_npm_package_ships_it():
    files = json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("files") or []
    assert "llms.txt" in files, files
