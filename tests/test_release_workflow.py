"""The release workflow publishes by OIDC trusted publishing and holds no stored credential.

`.github/workflows/release.yml` must keep `id-token: write` on both publishing jobs (`pypi`,
`npm`) -- without it PyPI and npm cannot mint the short-lived credential and provenance -- and must
never gain a `password:` input, a `*token*` input other than `id-token`, a `secrets.` reference or
a `TOKEN` variable: any of those means a long-lived credential has crept back in.

Plain-text checks, no YAML parser: the dev requirements stay pytest + build.
"""
import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"
PUBLISH_JOBS = ("pypi", "npm")


def job_block(text, job):
    """The lines of one job under `jobs:` (two-space indented key), or None."""
    lines = text.splitlines()
    try:
        start = lines.index("jobs:")
    except ValueError:
        return None
    block, inside = [], False
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            inside = line.strip() == job + ":"
            continue
        if re.match(r"^\S", line):
            break
        if inside:
            block.append(line)
    return block or None


def problems(text):
    found = []
    for job in PUBLISH_JOBS:
        block = job_block(text, job)
        if block is None:
            found.append("job %r missing" % job)
        elif not any(re.match(r"^\s+id-token:\s*write\s*(#.*)?$", line) for line in block):
            found.append("job %r lacks id-token: write" % job)
    for no, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if re.search(r"\bpassword\s*:", code, re.I):
            found.append("line %d: password input" % no)
        key = re.match(r"^\s*([A-Za-z0-9_-]*token[A-Za-z0-9_-]*)\s*:", code, re.I)
        if key and key.group(1).lower() != "id-token":
            found.append("line %d: token input %r" % (no, key.group(1)))
        if "TOKEN" in code:
            found.append("line %d: TOKEN" % no)
        if "secrets." in code:
            found.append("line %d: secrets reference" % no)
    return found


def test_release_workflow_uses_oidc_and_no_stored_credential():
    assert WORKFLOW.is_file(), "missing %s" % WORKFLOW
    assert problems(WORKFLOW.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("old, new", [
    ("id-token: write", "id-token: none"),                      # OIDC permission dropped
    ("        with:\n", "        with:\n          password: x\n"),
    ("        with:\n", "        with:\n          api-token: x\n"),
    ("    steps:\n", "    env:\n      NODE_AUTH_TOKEN: x\n    steps:\n"),
    ("    steps:\n", "    env:\n      X: ${{ secrets.X }}\n    steps:\n"),
])
def test_checker_catches_each_regression(old, new):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert old in text, old
    assert problems(text.replace(old, new, 1)) != []
