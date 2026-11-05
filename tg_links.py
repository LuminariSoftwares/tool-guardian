"""tg_links -- hint which tool produces another tool's required input (P141 S3).

The idea comes from the "needs-X" hint in MCP routers reviewed on Glama: when a tool requires an input
(``issue_id``) that another tool returns, say so, so the model calls the producer first. Only the idea is taken;
this module is rewritten from the idea, with no code or text from those routers.

    link(catalogue) -> {(server, tool): [(param, "server.tool"), ...]}

``catalogue`` is ``{server: [tool dict as tools/list returns it]}``. A required input name of a tool is linked to
another tool when that tool's outputSchema has a property of the same name (compared with case and ``_ - .``
removed, so ``issue_id`` == ``issueId``) or when the name appears as a word (or run of words, so ``repo_name``
== "repo name") in its description. A tool never links to itself. Each value list is sorted and de-duplicated;
only tools with at least one link appear as keys.

    python tg_links.py --selftest
"""
from __future__ import annotations

import re
import sys

_WORD = re.compile(r"[A-Za-z0-9]+")
_MAX_RUN = 4


def _norm(name):
    """Case- and separator-insensitive form of a parameter or property name."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _stem(word):
    """Plural-insensitive form: 'issues' and 'issue' compare equal."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _required(tool):
    schema = tool.get("inputSchema") if isinstance(tool, dict) else None
    req = schema.get("required") if isinstance(schema, dict) else None
    return [str(r) for r in req] if isinstance(req, list) else []


def _outputs(tool):
    schema = tool.get("outputSchema")
    props = schema.get("properties") if isinstance(schema, dict) else None
    return {_norm(k) for k in props} if isinstance(props, dict) else set()


def _phrases(description):
    """Every run of 1..4 consecutive description words, joined and normalised."""
    words = [w.lower() for w in _WORD.findall(description or "")]
    out = set()
    for i in range(len(words)):
        for n in range(1, _MAX_RUN + 1):
            if i + n <= len(words):
                out.add(_stem("".join(words[i:i + n])))
    return out


def link(catalogue):
    """Map each tool with a required input some other tool produces to its (param, "server.tool") producers."""
    producers = []
    for server in sorted(catalogue or {}):
        for tool in catalogue[server] or []:
            if isinstance(tool, dict) and tool.get("name"):
                producers.append((server, str(tool["name"]), _outputs(tool), _phrases(tool.get("description"))))
    result = {}
    for server in sorted(catalogue or {}):
        for tool in catalogue[server] or []:
            if not (isinstance(tool, dict) and tool.get("name")):
                continue
            me = (server, str(tool["name"]))
            found = set()
            for param in _required(tool):
                key = _norm(param)
                if not key:
                    continue
                for psrv, pname, outs, phrases in producers:
                    if (psrv, pname) == me:
                        continue
                    if key in outs or _stem(key) in phrases:
                        found.add((param, "%s.%s" % (psrv, pname)))
            if found:
                result[me] = sorted(found)
    return result


def _cat():
    return {"gh": [
        {"name": "list_issues", "description": "List issues in a repo.",
         "outputSchema": {"type": "object", "properties": {"issueId": {"type": "string"}}}},
        {"name": "get_issue", "description": "Get one issue.",
         "inputSchema": {"type": "object", "required": ["issue_id"]}},
        {"name": "find_repo", "description": "Search repositories; returns the repo_name of each match."},
        {"name": "clone_repo", "description": "Clone a repository.",
         "inputSchema": {"type": "object", "required": ["repo_name"]}},
        {"name": "echo", "description": "Echo; returns the text.",
         "inputSchema": {"type": "object", "required": ["text"]}}]}


def _t_camel():
    return ("issue_id", "gh.list_issues") in link(_cat())[("gh", "get_issue")]


def _t_description():
    return ("repo_name", "gh.find_repo") in link(_cat())[("gh", "clone_repo")]


def _t_self():
    return ("echo" not in {k[1] for k in link(_cat())}
            and all(p != "%s.%s" % k for k, v in link(_cat()).items() for _n, p in v))


def _t_only_linked_keys():
    return set(link(_cat())) == {("gh", "get_issue"), ("gh", "clone_repo")}


def _t_sorted_dedup():
    cat = _cat()
    cat["gh"].append({"name": "also", "description": "returns issue id and issue_id again",
                      "outputSchema": {"properties": {"issue_id": {}}}})
    got = link(cat)[("gh", "get_issue")]
    return got == sorted(set(got)) and ("issue_id", "gh.also") in got


def _t_deterministic():
    a = _cat()
    b = {"gh": list(reversed(a["gh"]))}
    return link(a) == link(b)


def _t_cross_server():
    cat = {"a": [{"name": "make", "outputSchema": {"properties": {"doc_id": {}}}}],
           "b": [{"name": "use", "inputSchema": {"required": ["docId"]}}]}
    return link(cat) == {("b", "use"): [("docId", "a.make")]}


def _t_malformed():
    bad = {"s": [{"name": "x", "inputSchema": None}, "junk", {"name": "y", "inputSchema": {"required": "no"}}]}
    return link(bad) == {}


def run_selftest():
    checks = [("output_schema_camel_snake", _t_camel), ("description_word", _t_description), ("no_self_link", _t_self),
              ("only_linked_tools_are_keys", _t_only_linked_keys), ("sorted_deduplicated", _t_sorted_dedup),
              ("deterministic_order", _t_deterministic), ("cross_server", _t_cross_server),
              ("malformed_input_tolerated", _t_malformed)]
    passed = 0
    for name, fn in checks:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  error %s: %s %s" % (name, type(exc).__name__, exc))
        passed += ok
        print(("PASS " if ok else "FAIL ") + name)
    print("tg_links selftest: %d checks, %d passed, %d failed" % (len(checks), passed, len(checks) - passed))
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(run_selftest())
    print(__doc__)
