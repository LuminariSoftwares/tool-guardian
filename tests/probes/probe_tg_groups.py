# @studio: QA | Overseer probe for tool-guardian's tg_groups.py -- written from the CONTRACT, not from the module
# @kind: cli
# @called_by: human | delegate_task acceptance (tgp2_acc_groups.bat)
"""py probe_tg_groups.py [dir-holding-tg_groups.py]   -> last line: probe_tg_groups: N checks, N passed, M failed"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "."))
import tg_groups as G  # noqa: E402


def _tool(name, pad=0):
    return {"name": name, "description": "does " + name + " " + "x" * pad,
            "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}}}


SERVERS = {
    "jobs": [_tool("image_generate", 300), _tool("image_status"), _tool("video_render", 900), _tool("music_make")],
    "n8n": [_tool("n8n_list_workflows"), _tool("n8n_get_workflow")],
    "empty": [],
}
CFG = {"image": ["jobs.image_*"], "video": ["jobs.video_render"], "flows": ["n8n"], "ghost": ["nope", "jobs.zzz*"]}


def c_est_tokens_is_ceil_of_json_chars():
    obj = {"k": "v" * 100}
    return G.est_tokens(obj) == math.ceil(len(json.dumps(obj)) / 3.5) and G.est_tokens(obj, 4.0) == math.ceil(len(json.dumps(obj)) / 4.0)


def c_default_is_one_group_per_server_sorted():
    g = G.build_groups(SERVERS)
    return ([x["group"] for x in g] == ["empty", "jobs", "n8n"]
            and g[1]["tools"] == ["jobs.image_generate", "jobs.image_status", "jobs.music_make", "jobs.video_render"]
            and g[1]["tool_count"] == 4 and g[0]["tool_count"] == 0 and g[0]["token_cost"] == 0
            and g[1]["token_cost"] == G.est_tokens(sorted(SERVERS["jobs"], key=lambda t: t["name"])))


def c_selectors():
    g = {x["group"]: x for x in G.build_groups(SERVERS, CFG)}
    # 2026-09-25: the "other" group (tg_groups 0.3.0-alpha.4 working tree) -- a tool no selector covers is never
    # hidden; jobs.music_make is the one tool CFG leaves uncovered, so it must land there.
    return (sorted(g) == ["flows", "ghost", "image", "other", "video"]
            and g["other"]["tools"] == ["jobs.music_make"]
            and g["image"]["tools"] == ["jobs.image_generate", "jobs.image_status"]
            and g["video"]["tools"] == ["jobs.video_render"]
            and g["flows"]["tools"] == ["n8n.n8n_get_workflow", "n8n.n8n_list_workflows"]
            and g["video"]["token_cost"] > g["image"]["token_cost"] > 0)


def c_unresolved_is_reported_not_dropped():
    g = {x["group"]: x for x in G.build_groups(SERVERS, CFG)}
    return (g["ghost"]["tools"] == [] and g["ghost"]["unresolved"] == ["nope", "jobs.zzz*"]
            and g["image"]["unresolved"] == [])


def c_resolve_pairs_and_unknown_group():
    pairs = G.resolve(SERVERS, CFG, "image")
    try:
        G.resolve(SERVERS, CFG, "missing")
        raised = ""
    except KeyError as exc:
        raised = str(exc)
    return pairs == [("jobs", "image_generate"), ("jobs", "image_status")] and "flows" in raised and "image" in raised


def c_render_has_costs_total_and_next_step():
    text = G.render(G.build_groups(SERVERS, CFG), router_cost=321)
    lines = text.splitlines()
    return (any(l.startswith("image") and "2 tools" in l and "tokens" in l for l in lines)
            and "321" in text and "NEXT STEP:" in lines[-1] and "activate_group(group=" in lines[-1])


def c_inputs_not_mutated_and_deterministic():
    before = json.dumps(SERVERS, sort_keys=True)
    a, b = G.build_groups(SERVERS, CFG), G.build_groups(SERVERS, CFG)
    return a == b and json.dumps(SERVERS, sort_keys=True) == before


def c_all_fallback_names_taken_gets_numbered_group():
    # 2026-09-25: other/ungrouped/ungrouped_tools all used as group names while a tool is uncovered used to raise
    # StopIteration. The uncovered tools must land in a numbered fallback (other_2, then other_3, ...), never vanish.
    cfg = {"other": ["jobs.image_generate"], "ungrouped": ["jobs.image_status"], "ungrouped_tools": ["n8n"]}
    names = [g["group"] for g in G.build_groups(SERVERS, cfg)]
    cfg2 = dict(cfg, other_2=["jobs.music_make"])
    g2 = {g["group"]: g for g in G.build_groups(SERVERS, cfg2)}
    return "other_2" in names and "other_3" in g2 and "jobs.video_render" in g2["other_3"]["tools"]


CHECKS = [
    ("est_tokens_is_ceil_of_json_chars", c_est_tokens_is_ceil_of_json_chars),
    ("default_is_one_group_per_server_sorted", c_default_is_one_group_per_server_sorted),
    ("selectors", c_selectors), ("unresolved_is_reported_not_dropped", c_unresolved_is_reported_not_dropped),
    ("resolve_pairs_and_unknown_group", c_resolve_pairs_and_unknown_group),
    ("render_has_costs_total_and_next_step", c_render_has_costs_total_and_next_step),
    ("inputs_not_mutated_and_deterministic", c_inputs_not_mutated_and_deterministic),
    ("all_fallback_names_taken_gets_numbered_group", c_all_fallback_names_taken_gets_numbered_group),
]


def main():
    passed = 0
    for name, fn in CHECKS:
        try:
            ok, why = fn() is True, ""
        except Exception as exc:  # noqa: BLE001
            ok, why = False, " (%s: %s)" % (type(exc).__name__, exc)
        print("  %s %s%s" % ("ok  " if ok else "FAIL", name, why))
        passed += 1 if ok else 0
    total = len(CHECKS)
    print("probe_tg_groups: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
