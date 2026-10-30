"""The terse list_capabilities description stays within 160 characters however many servers
are live, and says when some are not shown (docs/adr/0002: the model must know they exist)."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tool_guardian as tg  # noqa: E402


def live(n):
    return {"server%02d" % i: SimpleNamespace(status="ok", hint=lambda: "") for i in range(n)}


def test_thirty_servers_fit_160_and_say_how_many_are_hidden():
    desc = tg.terse_list_capabilities(live(30))
    assert len(desc) <= 160, len(desc)
    assert "+" in desc and "more" in desc
    shown = desc.split("Servers: ")[1].split(", +")[0].split(", ")
    assert shown and "+%d more (list_capabilities shows all)" % (30 - len(shown)) in desc


def test_a_short_list_is_shown_whole_with_no_more_marker():
    desc = tg.terse_list_capabilities(live(2))
    assert "server00, server01" in desc and "more" not in desc


def test_terse_tools_carry_the_cut_description(monkeypatch):
    monkeypatch.setattr(tg, "TERSE", True)
    monkeypatch.setattr(tg, "SKILLS_DIRS", "")
    tools = {t["name"]: t for t in tg.build_all_tools(live(30))}
    assert "more" in tools["list_capabilities"]["description"]
    assert len(tools["list_capabilities"]["description"]) <= 160
