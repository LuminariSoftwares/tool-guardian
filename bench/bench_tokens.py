#!/usr/bin/env python3
"""
Benchmark script for tool-guardian token usage.
Measures how many tokens a client pays vs router tools and output ladder compression.
"""

import os
import sys
import json
import argparse
import math

# Insert the directory of this file and its parent to sys.path for importing tool_guardian
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)
sys.path.insert(0, os.path.dirname(script_dir))

try:
    import tool_guardian as tg
except ImportError as e:
    print(f"Failed to import tool_guardian: {e}")
    sys.exit(1)

def count_tokens(text):
    """Count tokens in text using tiktoken or fallback method."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return (len(enc.encode(text)), "tiktoken:cl100k_base")
    except ImportError:
        # Fallback: estimate tokens as chars / 3.5
        return (math.ceil(len(text) / 3.5), "estimate:chars/3.5")

def load_catalogs(directory):
    """Load all catalog JSON files from directory sorted by server name."""
    catalogs = []
    for filename in sorted(os.listdir(directory)):
        if filename.endswith('.json'):
            filepath = os.path.join(directory, filename)
            with open(filepath, 'r') as f:
                catalog = json.load(f)
                # Only include server and tools
                catalogs.append({
                    "server": catalog["server"],
                    "tools": catalog["tools"]
                })
    return catalogs

def client_tools(tools):
    """Convert tool list to OpenAI function-calling format."""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}
            }
        })
    return result

def router_tools(catalogs):
    """Build router tools from catalogs."""
    backends = {}
    for c in catalogs:
        b = tg.Backend(c["server"], {"command": "none"})
        b.status = "ok"
        b.tools = c["tools"]
        b.error = ""
        backends[c["server"]] = b
    
    return client_tools(tg.build_all_tools(backends))

def measure(catalogs):
    """Measure token usage for catalogs."""
    # Get method string from count_tokens
    _, method = count_tokens("test")
    
    # Calculate tokens per server
    servers = []
    total_tools = 0
    full_tokens = 0
    
    for c in catalogs:
        tools = client_tools(c["tools"])
        json_str = json.dumps(tools)
        tools_count = len(c["tools"])
        tokens, _ = count_tokens(json_str)
        
        servers.append({
            "server": c["server"],
            "tools": tools_count,
            "tokens": tokens
        })
        
        total_tools += tools_count
        full_tokens += tokens
    
    # Calculate router tokens
    router_tools_list = router_tools(catalogs)
    router_json_str = json.dumps(router_tools_list)
    router_tokens, _ = count_tokens(router_json_str)
    
    # Calculate savings
    if full_tokens > 0:
        saved_pct = round(100 * (1 - router_tokens / full_tokens), 1)
    else:
        saved_pct = 0
    
    return {
        "method": method,
        "servers": servers,
        "total_tools": total_tools,
        "full_tokens": full_tokens,
        "router_tokens": router_tokens,
        "saved_pct": saved_pct
    }

def ladder_samples():
    """Generate deterministic samples for ladder compression testing."""
    # build_log: 3000 lines with one error on line 2990
    build_log_lines = []
    for i in range(1, 3001):
        if i == 2990:
            build_log_lines.append("[02990] compiling module_2990.c ... error: undefined reference to 'init_bus'")
        else:
            build_log_lines.append("[%05d] compiling module_%d.c ... ok" % (i, i))
    build_log = "\n".join(build_log_lines) + "\n[exit code: 1]"

    # json_array: JSON array with 400 elements
    items = []
    for i in range(400):
        items.append({
            "id": i,
            "name": "item-%d" % i,
            "status": ("ok" if i % 7 else "stale"),
            "size": i * 37 % 1000
        })
    json_array = json.dumps(items, indent=1)

    # unified_diff: Unified diff of one file with 3 hunks, properly formatted with diff --git header
    unified_diff_lines = ["diff --git a/src/app.py b/src/app.py", "index 3f2a1c4..9b8e7d6 100644", "--- a/src/app.py", "+++ b/src/app.py"]
    for hunk_num in range(3):
        line_num = 1 + hunk_num * 400
        unified_diff_lines.append("@@ -%d,201 +%d,201 @@" % (line_num, line_num))
        # Add 100 context lines before the change
        for i in range(100):
            unified_diff_lines.append(" context line %d of src/app.py -- unchanged code kept for reference" % (i + line_num))
        # Add one old value line and new value line
        unified_diff_lines.append("-old_value_%d = compute_old(%d)" % (line_num + 100, line_num + 100))
        unified_diff_lines.append("+new_value_%d = compute_new(%d)" % (line_num + 100, line_num + 100))
        # Add 100 context lines after the change
        for i in range(100):
            unified_diff_lines.append(" context line %d of src/app.py -- unchanged code kept for reference" % (i + line_num + 201))
    
    unified_diff = "\n".join(unified_diff_lines)

    # csv: CSV file with 1500 rows
    csv_lines = ["id,name,region,amount"]
    for i in range(1500):
        region = ("north","south","east","west")[i % 4]
        amount = "%d.%02d" % (i * 13 % 997, i % 100)
        csv_lines.append("%d,customer-%d,%s,%s" % (i, i, region, amount))
    csv = "\n".join(csv_lines)

    return [
        ("build_log", build_log),
        ("json_array", json_array),
        ("unified_diff", unified_diff),
        ("csv", csv)
    ]

def ladder(samples):
    """Apply tool-guardian's ladder compression to samples."""
    # Map sample names to tools
    tool_map = {
        "build_log": "bash",
        "json_array": "http_get", 
        "unified_diff": "git_diff",
        "csv": "query"
    }
    
    rows = []
    for name, text in samples:
        tool = tool_map[name]
        out = tg.tg_ladder.compress(text, tool=tool, is_error=False, cfg=None)
        rows.append({
            "name": name,
            "tool": tool,
            "before_chars": len(text),
            "after_chars": len(out["text"]),
            "rule": out["rule"],
            "lossy": bool(out["lossy"])
        })
    
    return rows

def render_markdown(measured, ladder_rows):
    """Render measurement results as markdown."""
    md = []
    md.append("# Tool-Guardian Token Benchmark\n")
    md.append(f"Token counting method: {measured['method']}\n")
    
    # Server table
    md.append("| server | tools | tokens (full schemas) |")
    md.append("|-------|------:|---------------------:|")
    total_tokens = 0
    for row in measured["servers"]:
        md.append(f"| {row['server']} | {row['tools']} | {row['tokens']} |")
        total_tokens += row["tokens"]
    md.append(f"| **Total** | **{measured['total_tools']}** | **{total_tokens}** |")
    
    # Summary
    md.append(f"\n**Every request:** {measured['full_tokens']} tokens of tool schemas without tool-guardian, {measured['router_tokens']} with it ({measured['saved_pct']}% less).")
    
    # Ladder table
    md.append("\n| output | tool | before (chars) | after (chars) | rule |")
    md.append("|--------|------|---------------:|--------------:|------|")
    for row in ladder_rows:
        md.append(f"| {row['name']} | {row['tool']} | {row['before_chars']} | {row['after_chars']} | {row['rule']} |")
    
    return "\n".join(md)

def main():
    parser = argparse.ArgumentParser(description='Benchmark tool-guardian token usage')
    parser.add_argument('--catalogs', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "catalogs"), help='Catalog directory (default: fixtures/catalogs)')
    parser.add_argument('--json', action='store_true', help='Output JSON instead of markdown')
    parser.add_argument('--selftest', action='store_true', help='Run self-test')
    
    args = parser.parse_args()
    
    if args.selftest:
        # Run self-tests
        def check_count_tokens():
            result = count_tokens("hello world")
            return result is not None
            
        def check_load_catalogs():
            catalogs = load_catalogs(os.path.join(os.path.dirname(__file__), "fixtures", "catalogs"))
            return len(catalogs) > 0
            
        def check_client_tools():
            result = client_tools([{"name": "test"}])
            return len(result) == 1
            
        def check_router_tools():
            result = router_tools([])
            return isinstance(result, list)
            
        def check_measure():
            catalogs = load_catalogs(os.path.join(os.path.dirname(__file__), "fixtures", "catalogs"))
            result = measure(catalogs)
            return isinstance(result, dict)
            
        def check_ladder_samples():
            samples = ladder_samples()
            return len(samples) == 4
            
        def check_ladder():
            samples = ladder_samples()
            rows = ladder(samples)
            return len(rows) == 4
            
        def check_render_markdown():
            result = render_markdown({"method": "test", "servers": [], "total_tools": 0, "full_tokens": 0, "router_tokens": 0, "saved_pct": 0}, [])
            return isinstance(result, str)
            
        checks = [
            ("count_tokens", check_count_tokens),
            ("load_catalogs", check_load_catalogs),
            ("client_tools", check_client_tools),
            ("router_tools", check_router_tools),
            ("measure", check_measure),
            ("ladder_samples", check_ladder_samples),
            ("ladder", check_ladder),
            ("render_markdown", check_render_markdown)
        ]
        
        passed = 0
        failed = 0
        
        for name, check in checks:
            try:
                if check():
                    passed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        
        print(f"bench_tokens selftest: {len(checks)} checks, {passed} passed, {failed} failed")
        sys.exit(1 if failed > 0 or len(checks) == 0 else 0)
    
    # Get catalog directory from command line or default
    catalogs_dir = args.catalogs
    
    # Load catalogs
    catalogs = load_catalogs(catalogs_dir)
    
    # Measure
    measured = measure(catalogs)
    
    # Generate ladder samples and process them
    samples = ladder_samples()
    ladder_rows = ladder(samples)
    
    if args.json:
        result = {
            "catalog": measured,
            "ladder": ladder_rows
        }
        print(json.dumps(result, indent=1))
    else:
        # Render markdown report
        markdown = render_markdown(measured, ladder_rows)
        print(markdown)

if __name__ == "__main__":
    main()