#!/usr/bin/env python3
"""Search by meaning (P110 S6) measured against keywords: recall@3 over the seven bench catalogues
(bench/fixtures/catalogs, 95 tools) and bench/fixtures/search_queries.json (40 queries).

    python bench/bench_search.py --url http://127.0.0.1:11434/api/embed --model nomic-embed-text --min 0.5

Prints exactly one line:  recall@3 keyword=<pct> semantic=<pct> blended=<pct> over 40 queries

* keyword  = what search_capabilities returns with TOOL_GUARDIAN_EMBED_URL unset
* semantic = tools ranked by cosine alone, cut at the minimum
* blended  = what search_capabilities returns with search by meaning on
A positive query counts when any expected server.tool is among the first three result lines; a negative query
counts when the search lists nothing. The embedding endpoint is called for real, so the numbers belong to the
model behind --url. Tool vectors are cached in a throwaway folder, not in ~/.tool-guardian.
"""
import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import tool_guardian as tg  # noqa: E402
import tg_embed  # noqa: E402

CATALOGS = os.path.join(HERE, "fixtures", "catalogs")
QUERIES = os.path.join(HERE, "fixtures", "search_queries.json")


def load_router():
    r = tg.Router("", options={})
    r._ceiling = None
    for fn in sorted(os.listdir(CATALOGS)):
        if fn.endswith(".json"):
            with open(os.path.join(CATALOGS, fn), encoding="utf-8") as fh:
                c = json.load(fh)
            b = tg.Backend(c["server"], {"command": "none"})
            b.tools = [dict(t) for t in c["tools"]]
            b.status = "ok"
            r.backends[c["server"]] = b
    return r


def first_three(text, known):
    """The first three result lines of a search answer as server.tool names."""
    names = []
    for line in text.splitlines():
        head = line.split(": ", 1)[0]
        if head in known:
            names.append(head)
    return names[:3]


def score(kind, expect, top):
    return (not top) if kind == "negative" else any(e in top for e in expect)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get(tg_embed.URL_ENV, ""), help="embedding endpoint (.../api/embed or .../embeddings)")
    ap.add_argument("--model", default=os.environ.get(tg_embed.MODEL_ENV) or tg_embed.DEFAULT_MODEL)
    ap.add_argument("--min", type=float, default=float(os.environ.get(tg_embed.MIN_ENV) or tg_embed.DEFAULT_MIN),
                    help="the cosine a tool needs to be listed (default %s)" % tg_embed.DEFAULT_MIN)
    a = ap.parse_args()
    if not a.url:
        ap.error("--url (or %s) is required" % tg_embed.URL_ENV)
    with open(QUERIES, encoding="utf-8") as fh:
        queries = json.load(fh)["queries"]
    router = load_router()
    known = {"%s.%s" % (n, t["name"]) for n, b in router.backends.items() for t in b.tools}
    entries = [[n, t["name"], t.get("description") or ""] for n, b in sorted(router.backends.items()) for t in b.tools]

    os.environ.pop(tg_embed.URL_ENV, None)
    os.environ[tg_embed.CACHE_ENV] = tempfile.mkdtemp(prefix="tg-embed-bench-")
    try:
        vectors = tg_embed.tool_vectors(entries, a.url, a.model)
        qvecs = tg_embed.embed([q["query"] for q in queries], a.url, a.model)
    except (tg_embed.EmbedError, ValueError) as e:
        print("bench_search: cannot embed: %s" % e, file=sys.stderr)
        return 1

    keyword = [first_three(router.search(q["query"]), known) for q in queries]
    sem = []
    for qv in qvecs:
        ranked = sorted(((tg_embed.cosine(qv, v), k) for k, v in vectors.items()), key=lambda c: (-c[0], c[1]))
        sem.append([k for c, k in ranked if c >= a.min][:3])
    os.environ[tg_embed.URL_ENV] = a.url
    os.environ[tg_embed.MODEL_ENV] = a.model
    os.environ[tg_embed.MIN_ENV] = repr(a.min)
    blended = [first_three(router.search(q["query"]), known) for q in queries]
    if router._embed_off_logged:
        print("bench_search: search by meaning fell back to keywords mid-run; the blended figure is not valid", file=sys.stderr)
        return 1

    def pct(tops):
        return 100.0 * sum(score(q["kind"], q["expect"], t) for q, t in zip(queries, tops)) / len(queries)
    print("recall@3 keyword=%.1f semantic=%.1f blended=%.1f over %d queries"
          % (pct(keyword), pct(sem), pct(blended), len(queries)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
