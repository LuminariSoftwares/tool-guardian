#!/usr/bin/env python3
"""Producer recall (P141 S3): does a "needs-X" link find the tool that produces a required input?

    python bench/bench_links.py

Prints exactly one line:  producer recall: baseline <x>% links <y>%

Over the recorded bench servers (bench/fixtures/catalogs, the material bench_search.py uses) and the hand-written
cases in bench/fixtures/link_cases.json (consumer tool, required input name, expected producer):

* baseline = the producer is among the first five results of the real router's search_capabilities when the
  query is the required input's name (keywords only; search by meaning is switched off)
* links    = the producer is in tg_links.link()'s list for that consumer and input name
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import tg_links  # noqa: E402
from bench_search import CATALOGS, load_router  # noqa: E402

CASES = os.path.join(HERE, "fixtures", "link_cases.json")


def top_names(text, known, n=5):
    """The first n result lines of a search answer as server.tool names."""
    names = []
    for line in text.splitlines():
        if line.split(": ", 1)[0] in known:
            names.append(line.split(": ", 1)[0])
    return names[:n]


def main():
    os.environ.pop("TOOL_GUARDIAN_EMBED_URL", None)
    with open(CASES, encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]
    catalogue = {}
    for fn in sorted(os.listdir(CATALOGS)):
        if fn.endswith(".json"):
            with open(os.path.join(CATALOGS, fn), encoding="utf-8") as fh:
                c = json.load(fh)
            catalogue[c["server"]] = c["tools"]
    router = load_router()
    known = {"%s.%s" % (s, t["name"]) for s, ts in catalogue.items() for t in ts}
    links = tg_links.link(catalogue)
    base = hit = 0
    for case in cases:
        for ref in (case["consumer"], case["producer"]):
            if ref not in known:
                print("bench_links: case names unknown tool %s" % ref, file=sys.stderr)
                return 1
        srv, tool = case["consumer"].split(".", 1)
        if case["producer"] in top_names(router.search(case["param"], limit=5), known):
            base += 1
        if any(p == case["param"] and prod == case["producer"] for p, prod in links.get((srv, tool), [])):
            hit += 1
    n = len(cases)
    print("producer recall: baseline %.1f%% links %.1f%%" % (100.0 * base / n, 100.0 * hit / n))
    print("bench_links: %d cases, %d tools with links, %d link entries"
          % (n, len(links), sum(len(v) for v in links.values())), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
