"""Ranking and catalogue output must not move when features are added around them.

tests/fixtures/rank_golden.json was written from main BEFORE the 2026-10-14 "Later" items
(`python tests/test_rank_identity.py --write`). The test rebuilds the same output from the
same fixtures -- the seven bench catalogues and test_rank_ceiling's TOOLS -- and asserts it
is identical, string for string: tg_rank.score per tool and query, search(), catalogue().
Fake backends: no processes."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tool_guardian as tg  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "rank_golden.json"
CATALOGS = ROOT / "bench" / "fixtures" / "catalogs"
QUERIES = ["read file", "create issue", "search", "list directory", "where is restore_head defined",
           "which symbol in the code graph", "browser click", "memory graph entities", "workflow",
           "pull request review", "think step", "rows", "x"]


def _router(servers):
    r = tg.Router("", options={})
    r._ceiling = None
    for name, (spec, tools) in servers.items():
        b = tg.Backend(name, spec)
        b.tools = [dict(t) for t in tools]
        b.status = "ok"
        r.backends[name] = b
    return r


def _fixture_sets():
    bench = {}
    for f in sorted(os.listdir(CATALOGS)):
        if f.endswith(".json"):
            d = json.loads((CATALOGS / f).read_text(encoding="utf-8"))
            bench[d["server"]] = ({"command": "none"}, d["tools"])
    from test_rank_ceiling import TOOLS
    ceiling = {n: ({"command": "none"}, t) for n, t in TOOLS.items()}
    hinted = {n: ({"command": "none", "hints": ["defined", "where", "function", "file"]}
                  if n == "ops-scripts" else {"command": "none"}, t) for n, t in TOOLS.items()}
    return {"bench": bench, "rank_ceiling": ceiling, "rank_ceiling_hinted": hinted}


def snapshot() -> dict:
    out = {}
    for label, servers in _fixture_sets().items():
        r = _router(servers)
        entry = {"catalogue": r.catalogue(), "per_server": {}, "search": {}, "scores": {}}
        for name in sorted(servers):
            entry["per_server"][name] = r.catalogue(name)
        for q in QUERIES:
            entry["search"][q] = r.search(q)
            if tg.tg_rank is not None:
                toks = tg.tg_rank.tokens_of(q)
                entry["scores"][q] = {
                    "%s.%s" % (n, t.get("name")): tg.tg_rank.score(
                        {"name": t.get("name"), "description": tg.short(t.get("description")).lower()},
                        n, toks, spec.get("hints"))
                    for n, (spec, tools) in sorted(servers.items()) for t in tools}
        out[label] = entry
    return out


def test_rank_fixtures_produce_identical_output():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    now = snapshot()
    assert sorted(now) == sorted(golden)
    for label in golden:
        for part in ("catalogue", "per_server", "search", "scores"):
            assert now[label][part] == golden[label][part], (label, part)


if __name__ == "__main__":
    if "--write" in sys.argv:
        GOLDEN.write_text(json.dumps(snapshot(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("wrote", GOLDEN)
