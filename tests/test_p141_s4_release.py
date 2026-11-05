"""P141 S4: tool-guardian 0.10.0 release prep -- versions agree, the P141 modules are packaged,
and tg_poison's built-in pattern copy matches tg_poison_patterns.json."""
import json
import re
from pathlib import Path

import tg_poison

ROOT = Path(__file__).resolve().parent.parent


def test_versions_agree():
    py = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert (ROOT / "server.json").read_text(encoding="utf-8").count('"version": "%s"' % py) == 2
    assert '__version__ = "%s"' % py in (ROOT / "tool_guardian.py").read_text(encoding="utf-8")


def test_p141_modules_are_packaged():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    modules = re.findall(r'"(\w+)"', re.search(r"py-modules = \[([^\]]*)\]", pyproject).group(1))
    files = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"]
    for name in ("tg_poison", "tg_intent", "tg_links"):
        assert name in modules and name + ".py" in files, name
    assert "tg_poison_patterns.json" in files


def test_builtin_patterns_match_the_json():
    with open(tg_poison.DEFAULT_PATTERNS, encoding="utf-8") as fh:
        assert json.loads(tg_poison.BUILTIN_PATTERNS) == json.load(fh)


def test_missing_json_falls_back_to_builtin(monkeypatch, tmp_path):
    want = tg_poison.load_patterns()
    monkeypatch.setattr(tg_poison, "DEFAULT_PATTERNS", str(tmp_path / "absent.json"))
    assert tg_poison.load_patterns() == want and len(want) == 7
