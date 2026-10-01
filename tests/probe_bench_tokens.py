"""Overseer's contract probe for bench_tokens.py (P36 B1, tool-guardian). Written before the module.
usage: python probe_bench_tokens.py <dir holding bench_tokens.py>   -> prints `probe_bench_tokens: N checks, ...`"""
import importlib.util, json, os, subprocess, sys, tempfile

HERE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
checks = []
def check(name, fn):
    try:
        ok = fn() is True
    except Exception as exc:  # noqa: BLE001
        print("  %s: threw %s: %s" % (name, type(exc).__name__, exc)); ok = False
    checks.append(ok); print("  %s %s" % ("ok  " if ok else "FAIL", name))

spec = importlib.util.spec_from_file_location("bench_tokens", os.path.join(HERE, "bench_tokens.py"))
bt = importlib.util.module_from_spec(spec); spec.loader.exec_module(bt)
res = {}
def _measure():
    res["m"] = bt.measure(bt.load_catalogs(os.path.join(HERE, "fixtures", "catalogs")))
    return True
check("measure_runs_on_fixtures", _measure)
m = res.get("m") or {}
check("seven_servers_95_tools", lambda: len(m.get("servers", [])) == 7 and m.get("total_tools") == 95)
check("servers_sorted_with_counts", lambda: [s["server"] for s in m["servers"]] == sorted(s["server"] for s in m["servers"]) and all(s["tools"] > 0 and s["tokens"] > 0 for s in m["servers"]))
check("router_is_a_small_fraction", lambda: m["full_tokens"] > 10 * m["router_tokens"] and m["router_tokens"] > 200)
check("saved_pct_matches", lambda: abs(m["saved_pct"] - round(100 * (1 - m["router_tokens"] / m["full_tokens"]), 1)) < 0.05)
check("method_named", lambda: m.get("method") in ("tiktoken:cl100k_base", "estimate:chars/3.5"))
check("router_tools_are_the_router_surface", lambda: {t["function"]["name"] for t in bt.router_tools(bt.load_catalogs(os.path.join(HERE, "fixtures", "catalogs")))} >= {"list_capabilities", "search_capabilities", "describe_tool", "call_tool"})
check("client_tools_openai_shape", lambda: bt.client_tools([{"name": "x", "description": "d", "inputSchema": {"type": "object", "properties": {"a": {"type": "string"}}}}]) == [{"type": "function", "function": {"name": "x", "description": "d", "parameters": {"type": "object", "properties": {"a": {"type": "string"}}}}}])
lad = []
def _ladder():
    lad.extend(bt.ladder(bt.ladder_samples())); return True
check("ladder_runs", _ladder)
check("ladder_four_samples_shrink", lambda: [r["name"] for r in lad] == ["build_log", "json_array", "unified_diff", "csv"] and all(r["after_chars"] < r["before_chars"] for r in lad))
check("ladder_samples_deterministic", lambda: bt.ladder_samples() == bt.ladder_samples())
check("markdown_has_totals", lambda: "95" in bt.render_markdown(m, lad) and "%" in bt.render_markdown(m, lad))
def _cli_json_other_cwd():
    with tempfile.TemporaryDirectory() as d:
        out = subprocess.run([sys.executable, os.path.join(HERE, "bench_tokens.py"), "--json"], cwd=d, capture_output=True, text=True, timeout=120)
    data = json.loads(out.stdout)
    return out.returncode == 0 and data["catalog"]["total_tools"] == 95 and len(data["ladder"]) == 4
check("cli_json_from_another_cwd_uses_default_fixtures", _cli_json_other_cwd)
def _selftest():
    out = subprocess.run([sys.executable, os.path.join(HERE, "bench_tokens.py"), "--selftest"], capture_output=True, text=True, timeout=120)
    return out.returncode == 0 and "bench_tokens selftest:" in out.stdout and " 0 failed" in out.stdout
check("selftest_passes_and_prints_count", _selftest)
passed = sum(checks)
print("probe_bench_tokens: %d checks, %d passed, %d failed" % (len(checks), passed, len(checks) - passed))
sys.exit(0 if passed == len(checks) and checks else 1)
