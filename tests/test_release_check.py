"""The release check: the tag follows PyPI, npm publishes only a version the registry lacks.

PyPI and npm are independent version lines (`.github/scripts/release_check.py`). Registry lookups
are mocked: no test touches the network.
"""
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "release_check.py"


@pytest.fixture(scope="module")
def rc():
    spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_plan(rc, tag, pyproject, npm, on_pypi=(), on_npm=(), publish=True):
    return rc.plan(tag, pyproject, npm, publish,
                   pypi_has=lambda v: v in on_pypi, npm_has=lambda v: v in on_npm)


def test_independent_versions_pass(rc):
    p = run_plan(rc, "v0.11.0", "0.11.0", "0.1.0-beta.1")
    assert p["errors"] == []
    assert p["pypi"] and p["npm"] and p["prerelease"]


def test_tag_must_equal_pyproject(rc):
    p = run_plan(rc, "v0.12.0", "0.11.0", "0.1.0-beta.1")
    assert any("v0.12.0" in e and "0.11.0" in e for e in p["errors"])


def test_tag_is_pep440_as_written(rc):
    assert run_plan(rc, "v1.0.0rc1", "1.0.0rc1", "1.0.0")["errors"] == []
    assert run_plan(rc, "v1.0.0-rc1", "1.0.0rc1", "1.0.0")["errors"] != []


def test_npm_version_already_on_registry_skips_npm(rc):
    p = run_plan(rc, "v0.11.0", "0.11.0", "0.1.0-beta.1", on_npm={"0.1.0-beta.1"})
    assert p["errors"] == [] and p["pypi"] and not p["npm"]
    assert any("already on npm" in n for n in p["notices"])


def test_pypi_only_patch_passes_and_skips_npm(rc):
    # pyproject bumped 0.11.0 -> 0.11.1, package.json unchanged (its version is already on npm)
    p = run_plan(rc, "v0.11.1", "0.11.1", "0.1.0-beta.1",
                 on_pypi={"0.11.0"}, on_npm={"0.1.0-beta.1"})
    assert p["errors"] == [] and p["pypi"] and not p["npm"]


def test_neither_version_new_is_an_error(rc):
    p = run_plan(rc, "v0.11.0", "0.11.0", "0.1.0-beta.1",
                 on_pypi={"0.11.0"}, on_npm={"0.1.0-beta.1"})
    assert any("nothing to publish" in e for e in p["errors"])


def test_nothing_to_publish_only_warns_on_a_dry_run(rc):
    p = run_plan(rc, "v0.11.0", "0.11.0", "0.1.0-beta.1",
                 on_pypi={"0.11.0"}, on_npm={"0.1.0-beta.1"}, publish=False)
    assert p["errors"] == [] and any("nothing to publish" in w for w in p["warnings"])
    assert not p["pypi"] and not p["npm"]


def test_plain_npm_version_is_not_a_prerelease(rc):
    assert run_plan(rc, "v0.11.0", "0.11.0", "0.2.0")["prerelease"] is False


class FakeRun:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.mark.parametrize("result, expected", [
    (FakeRun(0, "0.1.0-beta.1\n"), True),                          # published
    (FakeRun(0, ""), False),                                       # older npm: prints nothing
    (FakeRun(1, "", "npm error code E404\nnpm error 404 No match found"), False),
])
def test_npm_lookup(rc, monkeypatch, result, expected):
    monkeypatch.setattr(rc.subprocess, "run", lambda *a, **k: result)
    assert rc.npm_has("dsh-tool-guardian", "0.1.0-beta.1") is expected


def test_npm_lookup_fails_loudly_on_other_errors(rc, monkeypatch):
    monkeypatch.setattr(rc.subprocess, "run", lambda *a, **k: FakeRun(1, "", "npm error code ECONNRESET"))
    with pytest.raises(RuntimeError):
        rc.npm_has("dsh-tool-guardian", "0.1.0-beta.1")


def test_reads_versions_from_files(rc, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["x"]\n\n[project]\nname = "tool-guardian"\nversion = "0.11.0"\n',
        encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name": "dsh-tool-guardian", "version": "0.1.0-beta.1"}',
                                           encoding="utf-8")
    assert rc.read_pyproject(tmp_path / "pyproject.toml") == ("tool-guardian", "0.11.0")
    assert rc.read_package(tmp_path / "package.json") == ("dsh-tool-guardian", "0.1.0-beta.1")
