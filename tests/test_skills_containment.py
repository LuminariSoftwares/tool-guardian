"""Skills directory containment (2026-10-24): the skills scanner must not follow a link, junction
or `..` out of the configured skills root. Each escape test failed on main before the fix."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402


def _skill(directory, name, body="body"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text("---\nname: %s\ndescription: %s skill\n---\n%s\n"
                                        % (name, name, body), encoding="utf-8")


def _link(target, where):
    try:
        os.symlink(target, where, target_is_directory=Path(target).is_dir())
    except (OSError, NotImplementedError) as e:
        pytest.skip("cannot create a symlink here: %s" % e)


@pytest.fixture
def tree(tmp_path):
    outside = tmp_path / "outside"
    _skill(outside / "evil", "evil-dir", "OUTSIDE-SECRET")
    (outside / "target.md").write_text("---\nname: evil-file\n---\nOUTSIDE-SECRET\n", encoding="utf-8")
    skills = tmp_path / "skills"
    _skill(skills / "good", "good")
    _skill(skills / "group" / "nested", "nested")
    return tmp_path, skills, outside


def _names(skills):
    return sorted(s["name"] for s in tg.scan_skills(str(skills)))


def test_normal_and_nested_skills_are_still_found(tree):
    _, skills, _ = tree
    assert _names(skills) == ["good", "nested"]


def test_symlinked_skill_dir_outside_the_root_is_skipped(tree, capsys):
    _, skills, outside = tree
    _link(outside / "evil", skills / "link_dir")
    assert _names(skills) == ["good", "nested"]
    assert "link_dir" in capsys.readouterr().err


def test_symlinked_group_dir_outside_the_root_is_skipped(tree, capsys):
    _, skills, outside = tree
    _link(outside, skills / "link_group")
    assert _names(skills) == ["good", "nested"]
    assert "link_group" in capsys.readouterr().err


def test_skill_md_symlinked_to_a_file_outside_is_skipped(tree, capsys):
    _, skills, outside = tree
    (skills / "filelink").mkdir()
    _link(outside / "target.md", skills / "filelink" / "SKILL.md")
    assert _names(skills) == ["good", "nested"]
    assert "filelink" in capsys.readouterr().err


def test_symlink_to_a_plain_file_is_not_a_skill(tree):
    _, skills, outside = tree
    _link(outside / "target.md", skills / "just_a_file")
    assert _names(skills) == ["good", "nested"]


def test_symlink_that_stays_inside_the_root_is_kept(tree):
    _, skills, _ = tree
    _link(skills / "good", skills / "alias")
    assert _names(skills) == ["good", "good", "nested"]


def test_dotdot_in_the_configured_root_resolves_to_the_root(tree):
    _, skills, _ = tree
    (skills / "sub").mkdir()
    assert _names(str(skills / "sub" / "..")) == ["good", "nested"]


def test_dotdot_cannot_be_used_to_leave_the_root_via_a_link(tree):
    _, skills, outside = tree
    _link(os.path.join("..", "outside", "evil"), skills / "dotdot_link")
    assert _names(skills) == ["good", "nested"]


def test_read_skill_never_returns_text_from_outside(tree):
    _, skills, outside = tree
    _link(outside / "evil", skills / "link_dir")
    (skills / "filelink").mkdir()
    _link(outside / "target.md", skills / "filelink" / "SKILL.md")
    old = tg.SKILLS_DIRS
    tg.SKILLS_DIRS = str(skills)
    try:
        router = tg.Router("")
        listing = router._handle_skill("list_skills", {})
        assert "evil" not in listing
        for name in ("evil-dir", "evil-file"):
            assert "OUTSIDE-SECRET" not in router._handle_skill("read_skill", {"name": name})
        assert "body" in router._handle_skill("read_skill", {"name": "good"})
    finally:
        tg.SKILLS_DIRS = old


def test_skills_report_counts_only_contained_skills(tree):
    tmp, skills, outside = tree
    _link(outside / "evil", skills / "link_dir")
    run = subprocess.run([sys.executable, str(ROOT / "tool_guardian.py"), "--skills-report"],
                         capture_output=True, text=True, timeout=60,
                         env=dict(os.environ, TOOL_GUARDIAN_SKILLS=str(skills), TOOL_GUARDIAN_HOME=str(tmp)))
    assert run.stdout.startswith("skills: 2 found"), run.stdout
    assert "link_dir" in run.stderr


@pytest.mark.skipif(os.name != "nt", reason="directory junctions exist only on Windows")
def test_junction_out_of_the_root_is_skipped_like_a_symlink(tree):
    _, skills, outside = tree
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(skills / "junc"), str(outside / "evil")],
                          capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here: %s" % made.stderr.strip())
    assert _names(skills) == ["good", "nested"]
