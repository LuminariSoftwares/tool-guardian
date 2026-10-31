"""P110 S6 (A2) acceptance: search by meaning, off by default, shipped only on a measurement.

Contract (C8; written by the overseer before the code, red on main):

tg_embed.py -- a new sibling module, standard library only, listed in pyproject.toml `py-modules`
and in package.json `files`:
- URL_ENV = "TOOL_GUARDIAN_EMBED_URL", MODEL_ENV = "TOOL_GUARDIAN_EMBED_MODEL",
  CACHE_ENV = "TOOL_GUARDIAN_EMBED_CACHE", MIN_ENV = "TOOL_GUARDIAN_EMBED_MIN";
  DEFAULT_MIN: a float in (0, 1) -- the cosine a tool needs to be listed at all;
  KEYWORD_WEIGHT = 0.01 -- meaning first: rank = cosine + KEYWORD_WEIGHT * keyword_score (keyword only breaks ties).
  (Plan C8 said keyword*1.0 + cosine*4.0; measured 45-47.5 vs keyword 35 on real embeddings, under the +15 gate,
  because keyword substring noise lists tools for unrelated queries and outranks paraphrase hits. TJ 2026-10-31
  chose meaning first.)
- class EmbedError(Exception).
- endpoint_kind(url) -> "ollama" when the URL path ends with "/api/embed", "openai" when it ends with
  "/embeddings" (e.g. "/v1/embeddings"); anything else raises ValueError.
- embed(texts, url, model, timeout=30.0) -> list of vectors, ONE POST: Ollama body {"model", "input": texts}
  -> response "embeddings"; OpenAI body {"model", "input": texts} -> response "data"[].embedding put back in
  "index" order. Any failure (connection, HTTP status, bad JSON, wrong count) raises EmbedError.
- cosine(a, b) -> float; 0.0 when either vector is all zeros; ValueError on different lengths.
- tool_text(server, tool) -> "<server>.<name>: <description>" (description may be missing -> "").
- schema_hash(model, entries) -> sha256 hex of
  json.dumps({"model": model, "tools": sorted(entries)}, sort_keys=True, separators=(",", ":")),
  entries = [server, name, description] lists.
- cache_dir() -> $TOOL_GUARDIAN_EMBED_CACHE, else ~/.tool-guardian/embed_cache.
- tool_vectors(entries, url, model, cache=None) -> {"server.name": vector}: reads <cache>/<schema_hash>.json
  when it exists (no request); otherwise ONE embed() call over tool_text of every entry, then writes that file.

Router.search (the seam in tool_guardian.py, imported through _optional like tg_rank):
- TOOL_GUARDIAN_EMBED_URL unset: exactly today's output, and no request anywhere (tests/test_rank_identity.py
  must stay green unchanged).
- set: the query is embedded (one call), the visible tools' vectors come from tool_vectors(); a tool is listed
  ONLY when its cosine >= $TOOL_GUARDIAN_EMBED_MIN (default DEFAULT_MIN) -- a keyword hit alone lists nothing,
  so an unrelated query can answer "no tools match"; the rank is cosine + KEYWORD_WEIGHT * keyword_score, ties by
  "server.tool"; every other line of the output is unchanged.
- any EmbedError / ValueError: the keyword output, byte for byte, and ONE stderr line containing
  "search by meaning off" for the life of that Router -- not one per search.

bench/bench_search.py [--url URL] [--model M] [--min X]: over the seven bench catalogues and
bench/fixtures/search_queries.json (40 queries: 20 synonym, 10 exact, 10 negative), prints exactly one line
  recall@3 keyword=<pct> semantic=<pct> blended=<pct> over 40 queries
(pct with one decimal). A positive query counts when any expected "server.tool" is among the first three result
lines; a negative one when the search lists nothing (semantic = tools ranked by cosine alone, cut at the same
minimum; blended = what search_capabilities returns with search by meaning on). Merge gate (plan): blended >= keyword + 15.0 on a real local embedding model, and the identity snapshot
unchanged; otherwise the PR closes unmerged and the CHANGELOG records the numbers.
"""
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402

QUERIES = ROOT / "bench" / "fixtures" / "search_queries.json"

# A deterministic stand-in for an embedding model: words are folded onto concepts, then hashed into 32 dims.
SYNONYMS = {"memo": "note", "notes": "note", "persist": "create", "save": "create", "store": "create",
            "add": "create", "jot": "create", "reading": "read", "fetch": "read", "open": "read"}


def fake_vector(text):
    v = [0.0] * 32
    for w in re.findall(r"[a-z]+", text.lower()):
        w = SYNONYMS.get(w, w)
        if len(w) > 2:
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 32] += 1.0
    return v


class Stub:
    """Embedding server on 127.0.0.1: /api/embed (Ollama) and /v1/embeddings (OpenAI, answered in reverse order)."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append((self.path, body))
                texts = body.get("input") or []
                if self.path == "/api/embed":
                    out = {"model": body.get("model"), "embeddings": [fake_vector(t) for t in texts]}
                elif self.path == "/v1/embeddings":
                    data = [{"object": "embedding", "index": i, "embedding": fake_vector(t)} for i, t in enumerate(texts)]
                    out = {"object": "list", "data": list(reversed(data))}
                elif self.path == "/broken/api/embed":
                    self.send_response(500)
                    self.end_headers()
                    return
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def closed_port_url():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return "http://127.0.0.1:%d/api/embed" % port


def embed_mod():
    import importlib
    return importlib.import_module("tg_embed")


def router(servers):
    r = tg.Router("", options={})
    r._ceiling = None
    for name, tools in servers.items():
        b = tg.Backend(name, {"command": "none"})
        b.tools = [dict(t) for t in tools]
        b.status = "ok"
        r.backends[name] = b
    return r


SERVERS = {"notes": [{"name": "add_note", "description": "Create a note with a title", "inputSchema": {"type": "object"}},
                     {"name": "list_notes", "description": "List every note", "inputSchema": {"type": "object"}}],
           "files": [{"name": "read_file", "description": "Read a file from disk", "inputSchema": {"type": "object"}},
                     {"name": "delete_file", "description": "Remove a path", "inputSchema": {"type": "object"}}]}


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in ("TOOL_GUARDIAN_EMBED_URL", "TOOL_GUARDIAN_EMBED_MODEL", "TOOL_GUARDIAN_EMBED_MIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_CACHE", str(tmp_path / "cache"))
    return tmp_path


# 1
def test_tg_embed_is_a_stdlib_sibling_module_that_ships():
    src = (ROOT / "tg_embed.py").read_text(encoding="utf-8")
    # sys.stdlib_module_names is 3.10+; on 3.9 a module counts as stdlib when it is not installed under site-packages
    std = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}

    def is_std(name):
        if name in std or name in sys.builtin_module_names:
            return True
        import importlib.util
        spec = importlib.util.find_spec(name)
        origin = (spec.origin or "") if spec else "site-packages"
        return "site-packages" not in origin and "dist-packages" not in origin
    for n in ast.walk(ast.parse(src)):
        names = [a.name for a in n.names] if isinstance(n, ast.Import) else (
            [n.module] if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 else [])
        for nm in names:
            assert is_std(nm.split(".")[0]), "non-stdlib import: %s" % nm
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tg_embed"' in pyproject.split("py-modules", 1)[1].split("\n", 1)[0]
    files = json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("files") or []
    assert "tg_embed.py" in files
    assert tg.tg_embed is not None, "tool_guardian must import tg_embed through _optional"


# 2
def test_constants_endpoint_kind_and_cosine():
    m = embed_mod()
    assert (m.URL_ENV, m.MODEL_ENV, m.CACHE_ENV, m.MIN_ENV) == (
        "TOOL_GUARDIAN_EMBED_URL", "TOOL_GUARDIAN_EMBED_MODEL", "TOOL_GUARDIAN_EMBED_CACHE", "TOOL_GUARDIAN_EMBED_MIN")
    assert m.KEYWORD_WEIGHT == 0.01 and isinstance(m.DEFAULT_MIN, float) and 0.0 < m.DEFAULT_MIN < 1.0
    assert m.endpoint_kind("http://127.0.0.1:11434/api/embed") == "ollama"
    assert m.endpoint_kind("https://example.org/v1/embeddings") == "openai"
    assert m.endpoint_kind("http://127.0.0.1:8080/embeddings") == "openai"
    for bad in ("http://127.0.0.1:11434", "http://127.0.0.1:11434/api/chat", "http://127.0.0.1/v1/chat/completions"):
        with pytest.raises(ValueError):
            m.endpoint_kind(bad)
    assert m.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert m.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert m.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    with pytest.raises(ValueError):
        m.cosine([1.0], [1.0, 2.0])


# 3
def test_embed_speaks_both_endpoint_shapes_in_one_post(stub):
    m = embed_mod()
    texts = ["create a note", "read a file", "delete a path"]
    a = m.embed(texts, stub.base + "/api/embed", "m1")
    b = m.embed(texts, stub.base + "/v1/embeddings", "m2")
    assert a == b == [fake_vector(t) for t in texts]  # the OpenAI answer came back reversed; index order restored
    assert [(p, body) for p, body in stub.requests] == [
        ("/api/embed", {"model": "m1", "input": texts}), ("/v1/embeddings", {"model": "m2", "input": texts})]


# 4
def test_embed_failures_raise_embed_error(stub):
    m = embed_mod()
    with pytest.raises(m.EmbedError):
        m.embed(["x"], closed_port_url(), "m")
    with pytest.raises(m.EmbedError):
        m.embed(["x"], stub.base + "/broken/api/embed", "m")
    with pytest.raises(m.EmbedError):
        m.embed(["x"], stub.base + "/nothing/embeddings", "m")


# 5
def test_tool_vectors_are_cached_by_schema_hash(stub, clean_env):
    m = embed_mod()
    cache = clean_env / "cache"
    entries = [["notes", "add_note", "Create a note"], ["files", "read_file", "Read a file"]]
    url = stub.base + "/api/embed"
    v1 = m.tool_vectors(entries, url, "m1", cache=str(cache))
    assert set(v1) == {"notes.add_note", "files.read_file"} and len(stub.requests) == 1
    assert sorted(stub.requests[0][1]["input"]) == ["files.read_file: Read a file", "notes.add_note: Create a note"]
    want = hashlib.sha256(json.dumps({"model": "m1", "tools": sorted(entries)}, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
    assert m.schema_hash("m1", entries) == want and (cache / (want + ".json")).is_file()
    assert m.tool_vectors(list(reversed(entries)), url, "m1", cache=str(cache)) == v1 and len(stub.requests) == 1
    m.tool_vectors([["notes", "add_note", "Create a note!"], entries[1]], url, "m1", cache=str(cache))
    m.tool_vectors(entries, url, "m2", cache=str(cache))
    assert len(stub.requests) == 3  # a changed description and a changed model each miss the cache
    assert m.tool_text("s", {"name": "t"}) == "s.t: "


# 6
def test_off_by_default_is_byte_identical_and_makes_no_request(stub, clean_env, monkeypatch, capsys):
    m = embed_mod()

    def never(*a, **k):
        raise AssertionError("search by meaning ran with TOOL_GUARDIAN_EMBED_URL unset")
    monkeypatch.setattr(m, "embed", never)
    monkeypatch.setattr(m, "tool_vectors", never)
    monkeypatch.setattr(tg, "tg_embed", m)
    on = router(SERVERS)
    queries = ["create note", "read file", "persist a memo", "list"]
    got = [on.search(q) for q in queries]
    monkeypatch.setattr(tg, "tg_embed", None)
    want = [router(SERVERS).search(q) for q in queries]
    assert got == want and stub.requests == [] and "search by meaning" not in capsys.readouterr().err


# 7
def test_on_a_tool_with_no_keyword_hit_is_found_by_meaning(stub, clean_env, monkeypatch):
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_URL", stub.base + "/api/embed")
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_MODEL", "m1")
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_MIN", "0.5")
    monkeypatch.setattr(tg, "tg_embed", embed_mod())
    r = router(SERVERS)
    keyword_only = router(SERVERS)
    out = r.search("persist a memo")
    assert "notes.add_note: Create a note with a title" in out.splitlines()
    assert "files.read_file" not in out and "files.delete_file" not in out
    monkeypatch.delenv("TOOL_GUARDIAN_EMBED_URL")
    assert "no tools match" in keyword_only.search("persist a memo")
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_URL", stub.base + "/api/embed")
    before = len(stub.requests)
    r.search("persist a memo")
    assert len(stub.requests) == before + 1  # the tool vectors were cached; only the query is embedded again


# 8
RANK_SERVERS = {"shop": [{"name": "storemanager", "description": "memorial store opening hours", "inputSchema": {"type": "object"}}],
                "notes": [{"name": "add_note", "description": "Create a note", "inputSchema": {"type": "object"}}]}


def test_on_the_rank_is_cosine_with_keyword_as_tiebreak(stub, clean_env, monkeypatch):
    # "store memo": shop.storemanager wins on keywords (store, memo as substrings) but means something else;
    # notes.add_note has no keyword hit and the same meaning (store->create, memo->note). Meaning first puts
    # add_note on top; the plan's old blend (keyword*1 + cosine*4) would not.
    m = embed_mod()
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_URL", stub.base + "/api/embed")
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_MIN", "0.0")
    monkeypatch.setattr(tg, "tg_embed", m)
    q = "store memo"
    out = router(RANK_SERVERS).search(q)
    lines = [ln.split(":", 1)[0] for ln in out.splitlines() if re.match(r"^[a-z]+\.[a-z_]+: ", ln)]
    toks = tg.tg_rank.tokens_of(q)
    qv = fake_vector(q)
    meaning_first, old_blend = {}, {}
    for srv, tools in RANK_SERVERS.items():
        for t in tools:
            kw = tg.tg_rank.score({"name": t["name"], "description": tg.short(t["description"], tg.RANK_DESC_WORDS).lower()},
                                  srv, toks, None)
            cos = m.cosine(qv, fake_vector(m.tool_text(srv, t)))
            key = "%s.%s" % (srv, t["name"])
            meaning_first[key] = cos + m.KEYWORD_WEIGHT * kw
            old_blend[key] = kw * 1.0 + cos * 4.0
    want = sorted(meaning_first, key=lambda k: (-meaning_first[k], k))
    old = sorted(old_blend, key=lambda k: (-old_blend[k], k))
    assert want != old, "fixture no longer separates the two formulas"
    assert lines == want == ["notes.add_note", "shop.storemanager"]
    assert out.splitlines()[-1].startswith("NEXT STEP: use call_tool")


# 8b
def test_on_a_keyword_hit_alone_lists_nothing(stub, clean_env, monkeypatch):
    # "no" is a substring of both notes tools (a keyword hit), but carries no meaning: cosine 0 -> nothing listed
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_URL", stub.base + "/api/embed")
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_MIN", "0.5")
    monkeypatch.setattr(tg, "tg_embed", embed_mod())
    assert "no tools match" in router(SERVERS).search("no")
    monkeypatch.delenv("TOOL_GUARDIAN_EMBED_URL")
    assert "notes.add_note" in router(SERVERS).search("no")


# 9
def test_a_dead_endpoint_falls_back_to_keywords_with_one_log_line(clean_env, monkeypatch, capsys):
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_URL", closed_port_url())
    monkeypatch.setattr(tg, "tg_embed", embed_mod())
    r = router(SERVERS)
    got = [r.search("create note"), r.search("read file")]
    err = capsys.readouterr().err
    monkeypatch.delenv("TOOL_GUARDIAN_EMBED_URL")
    monkeypatch.setattr(tg, "tg_embed", None)
    assert got == [router(SERVERS).search("create note"), router(SERVERS).search("read file")]
    assert err.count("search by meaning off") == 1


# 10
def test_bad_url_shape_also_falls_back(clean_env, monkeypatch, capsys):
    monkeypatch.setenv("TOOL_GUARDIAN_EMBED_URL", "http://127.0.0.1:1/api/chat")
    monkeypatch.setattr(tg, "tg_embed", embed_mod())
    out = router(SERVERS).search("read file")
    monkeypatch.setattr(tg, "tg_embed", None)
    monkeypatch.delenv("TOOL_GUARDIAN_EMBED_URL")
    assert out == router(SERVERS).search("read file") and "search by meaning off" in capsys.readouterr().err


# 11
def test_the_query_set_is_forty_and_every_expected_tool_exists():
    d = json.loads(QUERIES.read_text(encoding="utf-8"))
    q = d["queries"]
    kinds = [x["kind"] for x in q]
    assert len(q) == 40 and kinds.count("synonym") == 20 and kinds.count("exact") == 10 and kinds.count("negative") == 10
    names = set()
    for f in (ROOT / "bench" / "fixtures" / "catalogs").glob("*.json"):
        c = json.loads(f.read_text(encoding="utf-8"))
        names |= {"%s.%s" % (c["server"], t["name"]) for t in c["tools"]}
    assert len(names) == 95
    for x in q:
        assert (x["expect"] == []) == (x["kind"] == "negative")
        assert all(e in names for e in x["expect"]), x


# 12
def test_bench_search_prints_one_recall_line(stub, clean_env):
    env = dict(os.environ)
    env.pop("TOOL_GUARDIAN_EMBED_URL", None)
    r = subprocess.run([sys.executable, str(ROOT / "bench" / "bench_search.py"), "--url", stub.base + "/api/embed",
                        "--model", "m1", "--min", "0.5"], capture_output=True, text=True, timeout=120, cwd=str(ROOT), env=env)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert r.returncode == 0, r.stderr
    assert len(lines) == 1 and re.fullmatch(
        r"recall@3 keyword=\d{1,3}\.\d semantic=\d{1,3}\.\d blended=\d{1,3}\.\d over 40 queries", lines[0]), r.stdout
    assert len(stub.requests) >= 2  # the catalogue vectors and the queries were embedded
