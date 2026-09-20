# @studio: Guardian | tool-guardian group manifest -- tools grouped, each group priced in context tokens
# @kind: library
# @called_by: tool_guardian.Router.handle (list_groups_with_costs) | modules/tg_bridge.py op_groups
"""py tg_groups.py --servers SERVERS.json [--groups GROUPS.json] [--router-cost N]
py tg_groups.py --selftest

The group manifest: the tools behind the router arranged into named groups, each priced in
the context tokens its schemas would cost if loaded -- so an agent can decide "the image group,
not the video group". PURE functions: no I/O, no environment, inputs never mutated.

CONTRACT
    servers     {server: [mcp tool dict, ...]}; a tool's full name is "<server>.<tool name>"
    groups_cfg  None/{} -> one group per server.  {group: [selector]} -> exactly those groups.
                selector: "server" | "server.tool" | "server.prefix*" (fnmatch on the tool name);
                one that matches nothing is listed under "unresolved", never dropped.
    est_tokens(obj, chars_per_token=3.5) -> ceil(len(json.dumps(obj)) / cpt); [] costs 0
    build_groups(servers, groups_cfg=None, chars_per_token=3.5)
        -> [{"group","tools","tool_count","token_cost","unresolved"}] sorted by group
    resolve(servers, groups_cfg, group) -> [(server, tool)]; unknown group -> KeyError naming the known ones
    render(groups, router_cost) -> text for a model, last line an exact NEXT STEP

History: written from the P19 contract after an ox attempt (2026-09-19) lost its model stream
mid-run at 5 of 7 contract-probe checks. MIT licensed.
"""
from __future__ import annotations

import fnmatch
import json
import math
import sys

NEXT_STEP = ('NEXT STEP: call activate_group(group="<name>") for the ONE group this task needs, '
             "or call_tool(server, tool, args) to use a tool without loading its group.")


def est_tokens(obj, chars_per_token: float = 3.5) -> int:
    if obj == []:
        return 0
    return math.ceil(len(json.dumps(obj)) / chars_per_token)


def _match(servers: dict, selector: str) -> list:
    """[(server, tool dict)] for one selector; [] when it matches nothing."""
    if selector in servers:
        return [(selector, t) for t in servers[selector]]
    server, dot, pattern = selector.partition(".")
    if not dot or server not in servers:
        return []
    return [(server, t) for t in servers[server] if fnmatch.fnmatchcase(str(t.get("name", "")), pattern)]


def _members(servers: dict, groups_cfg: dict | None) -> dict:
    """{group: ([(server, tool dict)] sorted by full name, no duplicates, [unresolved selectors])}"""
    cfg = groups_cfg or {name: [name] for name in servers}
    out = {}
    for group, selectors in cfg.items():
        found, unresolved = {}, []
        for selector in selectors:
            hits = _match(servers, str(selector))
            if not hits and not (groups_cfg in (None, {}) and selector in servers):
                unresolved.append(selector)
            for server, tool in hits:
                found["%s.%s" % (server, tool.get("name", ""))] = (server, tool)
        out[group] = ([found[k] for k in sorted(found)], unresolved)
    return out


def build_groups(servers: dict, groups_cfg: dict | None = None, chars_per_token: float = 3.5) -> list:
    rows = []
    for group, (members, unresolved) in sorted(_members(servers, groups_cfg).items()):
        rows.append({"group": group,
                     "tools": ["%s.%s" % (s, t.get("name", "")) for s, t in members],
                     "tool_count": len(members),
                     "token_cost": est_tokens([t for _, t in members], chars_per_token),
                     "unresolved": unresolved})
    return rows


def resolve(servers: dict, groups_cfg: dict | None, group: str) -> list:
    members = _members(servers, groups_cfg)
    if group not in members:
        raise KeyError("no group named %r. Known groups: %s" % (group, ", ".join(sorted(members)) or "none"))
    return [(server, str(tool.get("name", ""))) for server, tool in members[group][0]]


def render(groups: list, router_cost: int) -> str:
    width = max([len(g["group"]) for g in groups] + [5])
    lines = []
    for g in groups:
        line = "%s  %d tools  ~%d tokens" % (g["group"].ljust(width), g["tool_count"], g["token_cost"])
        if g["unresolved"]:
            line += "  unresolved: " + ", ".join(str(u) for u in g["unresolved"])
        lines.append(line)
    lines.append("router alone: ~%d tokens; all groups loaded: ~%d tokens"
                 % (router_cost, sum(g["token_cost"] for g in groups)))
    lines.append(NEXT_STEP)
    return "\n".join(lines)


# ------------------------------------------------------------- selftest -----

def _tool(name, pad=0):
    return {"name": name, "description": "does %s %s" % (name, "x" * pad),
            "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}}}


def _fixture():
    servers = {"jobs": [_tool("video_render", 900), _tool("image_status"), _tool("image_generate", 300)],
               "n8n": [_tool("n8n_list_workflows")], "empty": []}
    cfg = {"image": ["jobs.image_*"], "video": ["jobs.video_render"], "flows": ["n8n"],
           "both": ["jobs.image_status", "jobs.image_*"], "ghost": ["nope", "jobs.zzz*", "n8n.missing"]}
    return servers, cfg


def _by_name(servers, cfg=None):
    return {g["group"]: g for g in build_groups(servers, cfg)}


def _t_est_tokens_matches_formula():
    obj = {"k": "v" * 100}
    return est_tokens(obj) == math.ceil(len(json.dumps(obj)) / 3.5) and est_tokens(obj, 4.0) == math.ceil(len(json.dumps(obj)) / 4.0)


def _t_empty_list_costs_zero():
    return est_tokens([]) == 0 and _by_name(_fixture()[0])["empty"]["token_cost"] == 0


def _t_default_one_group_per_server():
    servers, _ = _fixture()
    g = build_groups(servers)
    return ([x["group"] for x in g] == ["empty", "jobs", "n8n"] and all(x["unresolved"] == [] for x in g)
            and g[1]["tools"] == ["jobs.image_generate", "jobs.image_status", "jobs.video_render"])


def _t_selector_whole_server():
    return _by_name(*_fixture())["flows"]["tools"] == ["n8n.n8n_list_workflows"]


def _t_selector_single_tool():
    g = _by_name(*_fixture())["video"]
    return g["tools"] == ["jobs.video_render"] and g["tool_count"] == 1 and g["token_cost"] > 200


def _t_selector_prefix_glob():
    return _by_name(*_fixture())["image"]["tools"] == ["jobs.image_generate", "jobs.image_status"]


def _t_overlapping_selectors_do_not_duplicate():
    return _by_name(*_fixture())["both"]["tools"] == ["jobs.image_generate", "jobs.image_status"]


def _t_unresolved_selectors_listed():
    g = _by_name(*_fixture())["ghost"]
    return g["tools"] == [] and g["unresolved"] == ["nope", "jobs.zzz*", "n8n.missing"] and g["token_cost"] == 0


def _t_resolve_returns_pairs_in_order():
    return resolve(*_fixture(), "image") == [("jobs", "image_generate"), ("jobs", "image_status")]


def _t_resolve_unknown_group_keyerror_lists_names():
    try:
        resolve(*_fixture(), "missing")
    except KeyError as exc:
        return all(name in str(exc) for name in ("image", "video", "flows"))
    return False


def _t_render_last_line_is_next_step():
    text = render(build_groups(*_fixture()), 321).splitlines()
    return (text[-1] == NEXT_STEP and "321" in text[-2] and any(ln.startswith("ghost") and "unresolved: nope" in ln for ln in text)
            and any(ln.startswith("image") and "2 tools" in ln for ln in text))


def _t_inputs_are_not_mutated():
    servers, cfg = _fixture()
    before = json.dumps([servers, cfg], sort_keys=True)
    first, second = build_groups(servers, cfg), build_groups(servers, cfg)
    return first == second and json.dumps([servers, cfg], sort_keys=True) == before


CHECKS = [
    ("est_tokens_matches_formula", _t_est_tokens_matches_formula),
    ("empty_list_costs_zero", _t_empty_list_costs_zero),
    ("default_one_group_per_server", _t_default_one_group_per_server),
    ("selector_whole_server", _t_selector_whole_server),
    ("selector_single_tool", _t_selector_single_tool),
    ("selector_prefix_glob", _t_selector_prefix_glob),
    ("overlapping_selectors_do_not_duplicate", _t_overlapping_selectors_do_not_duplicate),
    ("unresolved_selectors_listed", _t_unresolved_selectors_listed),
    ("resolve_returns_pairs_in_order", _t_resolve_returns_pairs_in_order),
    ("resolve_unknown_group_keyerror_lists_names", _t_resolve_unknown_group_keyerror_lists_names),
    ("render_last_line_is_next_step", _t_render_last_line_is_next_step),
    ("inputs_are_not_mutated", _t_inputs_are_not_mutated),
]


def selftest() -> int:
    passed = 0
    for name, fn in CHECKS:
        try:
            ok = fn() is True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL %s (%s: %s)" % (name, type(exc).__name__, exc))
        else:
            print("  %s %s" % ("ok  " if ok else "FAIL", name))
        passed += 1 if ok else 0
    total = len(CHECKS)
    print("tg_groups selftest: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if total > 0 and passed == total else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--servers" not in argv:
        print(__doc__.split("\n\n")[0])
        return 2

    def load(flag):
        with open(argv[argv.index(flag) + 1], encoding="utf-8") as fh:
            return json.load(fh)

    groups_cfg = load("--groups") if "--groups" in argv else None
    cost = int(argv[argv.index("--router-cost") + 1]) if "--router-cost" in argv else 0
    print(render(build_groups(load("--servers"), groups_cfg), cost))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
