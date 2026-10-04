"""P45 B4 (C-Hints via tg_rank) and B6 (C-Ceiling via tg_ceiling), 2026-10-03. Fake backends: no processes."""
import pytest

import tool_guardian as tg

pytestmark = pytest.mark.skipif(tg.tg_rank is None or tg.tg_ceiling is None,
                                reason="tg_rank.py / tg_ceiling.py are not beside tool_guardian.py")

TOOLS = {
    "luminari-scripts": [{"name": "graph_defines", "description": "find the file and line where a function or class is defined"},
                         {"name": "stack_find", "description": "search the script index by keyword"}],
    "codebase-memory": [{"name": "search_graph", "description": "search the code graph for a symbol"},
                        {"name": "where_used", "description": "list callers of a function"}],
    "nocodb": [{"name": "list_records", "description": "list rows where a field matches"}],
}


def make_router(hints=None, options=None):
    r = tg.Router("", options=options or {})
    for name, tools in TOOLS.items():
        spec = {"command": "none"}
        if hints and name in hints:
            spec["hints"] = hints[name]
        b = tg.Backend(name, spec)
        b.tools = [dict(t) for t in tools]
        b.status = "ok"
        r.backends[name] = b
    return r


def first_hit(router, query):
    out = router.search(query)
    return [ln.split(":")[0] for ln in out.splitlines()[1:] if "." in ln.split(":")[0]][0]


def test_without_hints_order_is_the_old_scoring():
    r = make_router()
    # old rule: name substring +3, server equality +2, description substring +1.
    # list_records: "is" in name (+3), "where" and "is" in desc (+2) = 5; where_used: 3+1 = 4; graph_defines: 0+3 = 3.
    # The misranking hints exist to fix.
    assert first_hit(r, "where is restore_head defined") == "nocodb.list_records"


def test_hints_put_graph_defines_first_for_t1():
    r = make_router(hints={"luminari-scripts": ["defined", "where", "function", "file"]})
    assert first_hit(r, "where is restore_head defined") == "luminari-scripts.graph_defines"


def test_hints_put_search_graph_first_for_t2():
    r = make_router(hints={"codebase-memory": ["symbol", "graph", "callers"]})
    assert first_hit(r, "which symbol in the code graph") == "codebase-memory.search_graph"


def test_eight_discovery_calls_then_ninth_refused():
    r = make_router()
    for i in range(8):
        assert not r._handle("search_capabilities", {"query": "graph"}).startswith("Discovery limit reached")
    out = r._handle("list_capabilities", {})
    assert out == ("Discovery limit reached (8 calls without call_tool). Call a tool now with call_tool, "
                   "or answer with what you have.")


def test_call_tool_resets_the_ceiling():
    r = make_router()
    for _ in range(8):
        r._handle("describe_tool", {"server": "nocodb", "tool": "list_records"})
    r._ceiling.note("call_tool")            # what _handle does first on call_tool, without spawning a backend
    assert not r._handle("search_capabilities", {"query": "rows"}).startswith("Discovery limit reached")


def test_discovery_limit_option():
    r = make_router(options={"discoveryLimit": 2})
    r._handle("search_capabilities", {"query": "x"})
    r._handle("search_capabilities", {"query": "x"})
    assert r._handle("search_capabilities", {"query": "x"}).startswith("Discovery limit reached (2 calls")
