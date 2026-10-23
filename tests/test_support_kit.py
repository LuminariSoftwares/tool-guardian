"""The support kit stays in place: issue forms are valid, blank issues stay off, SUPPORT.md exists.

Docs and repo metadata are a contract with strangers who arrive from a launch post. This fails if
`config.yml` turns blank issues back on, drops the private security link or the question route,
if an issue form stops being a valid GitHub issue form (name, description, body; every input has an
id; ids unique; the "I ran the quick start" checkbox is required), or if `SUPPORT.md` disappears or
stops being linked from CONTRIBUTING.md. Nothing in the kit may promise a fix time.

PyYAML is a dev requirement only (requirements-dev.txt); the package itself has no dependencies.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
FORMS = ("bug_report.yml", "feature_request.yml", "question.yml")
SECURITY_URL = "https://github.com/LuminariSoftwares/tool-guardian/security/advisories/new"
# Form elements that carry no id (GitHub: markdown has no id; every other type needs one).
NO_ID = {"markdown"}
TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes"}


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_config_blank_issues_off_and_contact_links():
    config = load(TEMPLATES / "config.yml")
    assert config["blank_issues_enabled"] is False
    links = {link["name"]: link for link in config["contact_links"]}
    assert links["Security issue"]["url"] == SECURITY_URL
    question = links["Question"]
    assert "question" in question["about"].lower()
    assert "discussions" in question["about"].lower()
    for link in links.values():
        assert link["url"].startswith("https://") and link["about"]


@pytest.mark.parametrize("name", FORMS)
def test_issue_form_is_valid(name):
    form = load(TEMPLATES / name)
    assert form["name"] and form["description"]
    assert isinstance(form["labels"], list) and form["labels"]
    body = form["body"]
    assert isinstance(body, list) and body
    ids = []
    for element in body:
        assert element["type"] in TYPES, element
        if element["type"] in NO_ID:
            continue
        assert element.get("id"), "every field needs an id: %r" % (element,)
        assert re.fullmatch(r"[A-Za-z0-9_-]+", element["id"]), element["id"]
        ids.append(element["id"])
        assert element["attributes"]["label"] if element["type"] != "checkboxes" else True
    assert len(ids) == len(set(ids)), "duplicate field ids in %s" % name


def test_bug_report_fields_and_required_checkbox():
    form = load(TEMPLATES / "bug_report.yml")
    assert "needs-info" in form["labels"]
    by_id = {e["id"]: e for e in form["body"] if "id" in e}
    for field in ("version", "os", "python_version", "command", "output", "expected"):
        assert field in by_id, field
        assert by_id[field].get("validations", {}).get("required") is True, field
    assert "tool-guardian --version" in by_id["version"]["attributes"]["description"]
    boxes = by_id["quick_start"]
    assert boxes["type"] == "checkboxes"
    labels = {o["label"]: o for o in boxes["attributes"]["options"]}
    assert labels["I ran the quick start as written"].get("required") is True


def test_question_form_carries_question_label():
    assert "question" in load(TEMPLATES / "question.yml")["labels"]


def test_support_md_present_and_complete():
    path = ROOT / "SUPPORT.md"
    assert path.is_file(), "SUPPORT.md is part of the support kit"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Support\n")
    for needle in (
        "best-effort",
        "acknowledgement within a day",
        "private vulnerability reporting",
        "tool-guardian --version",
        "docs/adr/",
        "no Discord, mailing list or paid support",
    ):
        assert needle in text, needle
    assert (ROOT / "docs" / "adr").is_dir()


def test_contributing_links_support():
    assert "(SUPPORT.md)" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


def test_no_fix_time_promised():
    """Only the acknowledgement carries a time; a fix never does."""
    paths = [ROOT / "SUPPORT.md"] + [TEMPLATES / n for n in FORMS + ("config.yml",)]
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"\b(fix|fixed|resolve[ds]?|patch(?:ed)?)\b.{0,40}\bwithin\b|\bwithin\b.{0,40}\b(fix|resolution)\b", line, re.I):
                pytest.fail("%s promises a fix time: %s" % (path.name, line))
