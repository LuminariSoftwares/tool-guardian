"""Search by meaning (P110 S6): embeddings for ``search_capabilities``. Standard library only, off unless
TOOL_GUARDIAN_EMBED_URL is set.

Two endpoint shapes, told apart by the URL path:

* ``.../api/embed``   Ollama:  ``{"model", "input": [...]}`` -> ``{"embeddings": [[...], ...]}``
* ``.../embeddings``  OpenAI-compatible (``/v1/embeddings``): same request ->
  ``{"data": [{"index", "embedding"}, ...]}``

Each call is ONE POST for any number of texts. The tool vectors are cached on disk under a hash of the model and
the whole catalogue, so a search costs one embedding call (the query) and a catalogue change costs one more.
"""
import hashlib
import http.client
import json
import math
import os
import tempfile
import urllib.parse
import urllib.request

URL_ENV = "TOOL_GUARDIAN_EMBED_URL"
MODEL_ENV = "TOOL_GUARDIAN_EMBED_MODEL"
CACHE_ENV = "TOOL_GUARDIAN_EMBED_CACHE"
MIN_ENV = "TOOL_GUARDIAN_EMBED_MIN"
DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_MIN = 0.5        # the cosine a tool needs to be listed at all
KEYWORD_WEIGHT = 0.01    # rank = cosine + KEYWORD_WEIGHT * keyword_score: meaning first, keywords break ties


class EmbedError(Exception):
    """The embedding endpoint did not give back usable vectors."""


def endpoint_kind(url: str) -> str:
    """"ollama" for a path ending in /api/embed, "openai" for one ending in /embeddings; else ValueError."""
    path = urllib.parse.urlparse(url or "").path.rstrip("/")
    if path.endswith("/api/embed"):
        return "ollama"
    if path.endswith("/embeddings"):
        return "openai"
    raise ValueError("%s must end in /api/embed (Ollama) or /embeddings (OpenAI-compatible), got %r"
                     % (URL_ENV, url))


def _opener(url: str):
    """A loopback endpoint is never sent through a system proxy."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _vector(v) -> list:
    if (not isinstance(v, list) or not v
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
        raise EmbedError("the embedding endpoint returned something that is not a vector")
    return [float(x) for x in v]


def embed(texts: list, url: str, model: str, timeout: float = 30.0) -> list:
    """One POST, one vector per text, in the order of ``texts``. Any failure raises EmbedError
    (a URL of the wrong shape raises ValueError, as endpoint_kind does)."""
    kind = endpoint_kind(url)
    body = json.dumps({"model": model, "input": list(texts)}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with _opener(url).open(req, timeout=timeout) as resp:
            raw = resp.read()
    except (OSError, http.client.HTTPException) as e:   # URLError, HTTPError, timeout and refused connections
        raise EmbedError("embedding request to %s failed: %s" % (url, e))
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise EmbedError("embedding endpoint %s did not return JSON: %s" % (url, e))
    if not isinstance(doc, dict):
        raise EmbedError("embedding endpoint %s returned JSON that is not an object" % url)
    if kind == "ollama":
        vectors = doc.get("embeddings")
        if not isinstance(vectors, list):
            raise EmbedError("embedding endpoint %s returned no \"embeddings\"" % url)
    else:
        data = doc.get("data")
        if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
            raise EmbedError("embedding endpoint %s returned no \"data\"" % url)
        if sorted(d.get("index") for d in data if isinstance(d.get("index"), int)) != list(range(len(data))):
            raise EmbedError("embedding endpoint %s returned unusable \"index\" values" % url)
        vectors = [d.get("embedding") for d in sorted(data, key=lambda d: d["index"])]
    if len(vectors) != len(texts):
        raise EmbedError("asked for %d embeddings, got %d" % (len(texts), len(vectors)))
    return [_vector(v) for v in vectors]


def cosine(a: list, b: list) -> float:
    """Cosine similarity; 0.0 when either vector is all zeros; ValueError on different lengths."""
    if len(a) != len(b):
        raise ValueError("vectors differ in length: %d vs %d" % (len(a), len(b)))
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def tool_text(server: str, tool: dict) -> str:
    """What gets embedded for one tool: ``<server>.<name>: <description>``."""
    return "%s.%s: %s" % (server, tool.get("name") or "", tool.get("description") or "")


def schema_hash(model: str, entries: list) -> str:
    """sha256 over the model and every [server, name, description] entry, in any order."""
    blob = json.dumps({"model": model, "tools": sorted(entries)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_dir() -> str:
    return os.environ.get(CACHE_ENV) or os.path.join(os.path.expanduser("~"), ".tool-guardian", "embed_cache")


def tool_vectors(entries: list, url: str, model: str, cache: str = None) -> dict:
    """{"server.name": vector} for every [server, name, description] entry. Read from
    ``<cache>/<schema_hash>.json`` when it exists (no request); otherwise ONE embed() call, then written there."""
    keys = ["%s.%s" % (e[0], e[1]) for e in entries]
    path = os.path.join(cache or cache_dir(), schema_hash(model, entries) + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, dict) and all(k in cached for k in keys):
            return {k: cached[k] for k in keys}
    except (OSError, ValueError):
        pass   # no cache file, or an unreadable one: embed again and replace it
    if not entries:
        return {}
    vectors = embed([tool_text(e[0], {"name": e[1], "description": e[2]}) for e in entries], url, model)
    out = dict(zip(keys, vectors))
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass   # a cache that cannot be written costs one more call next time, never a failed search
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return out
