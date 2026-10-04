// P45 B1 (C-Parse) smoke, written BEFORE the module (2026-10-03). Fixtures are the real text tool calls qwen3-coder
// emitted in the tg / tg2 benches (extracted by script from the session logs, never retyped).
//   node tests/text_toolcalls_smoke.mjs [path/to/text_toolcalls.js]
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
const here = path.dirname(fileURLToPath(import.meta.url));
const modPath = path.resolve(process.argv[2] || path.join(here, "..", "text_toolcalls.js"));
const res = [];
const check = (n, c, why = "") => { res.push(!!c); console.log((c ? "ok   " : "FAIL ") + n + (c || !why ? "" : "  -- " + why)); };
let m;
try { m = await import(pathToFileURL(modPath).href); } catch (e) { check("module_loads", false, String(e && e.message)); }
const S = (props) => ({ type: "object", properties: props });
const SCHEMAS = {
  search_capabilities: S({ query: { type: "string" }, server: { type: "string" } }),
  list_capabilities: S({ server: { type: "string" } }),
  call_tool: S({ server: { type: "string" }, tool: { type: "string" }, args: { description: "object or JSON string" } }),
  "mcp__codebase-memory__search_graph": S({ project: { type: "string" }, query: { type: "string" } }),
  "mcp__luminari-scripts__table_for": S({ kind: { type: "string" } }),
  "mcp__luminari-scripts__lessons_search": S({ query: { type: "string" }, id: { type: "integer" } }),
  "mcp__luminari-scripts__list_gigs": S({}),
  "mcp__luminari-scripts__service_status": S({ service: { type: "string" } }),
  demo: S({ n: { type: "number" }, flag: { type: "boolean" }, obj: { type: "object" }, s: { type: "string" } }),
};
const NAMES = Object.keys(SCHEMAS);
if (m) {
  const fx = path.join(here, "fixtures", "textcalls");
  const files = fs.readdirSync(fx).filter((f) => f.endsWith(".txt")).sort();
  check("fixture_count_21", files.length === 21, String(files.length));
  for (const f of files) {
    const text = fs.readFileSync(path.join(fx, f), "utf8");
    const exp = JSON.parse(fs.readFileSync(path.join(fx, f.replace(/\.txt$/, ".expected.json")), "utf8"));
    const r = m.parseTextToolCalls(text, NAMES, SCHEMAS);
    const ok = JSON.stringify(r.calls) === JSON.stringify(exp.calls) && r.malformed.length === 0
      && !r.rest.includes("<function=") && !r.rest.includes("</tool_call>") && r.rest.trim().startsWith(exp.rest_starts_with);
    check("fixture " + f, ok, JSON.stringify(r).slice(0, 220));
  }
  const two = "a\n<tool_call>\n<function=demo>\n<parameter=n>\n3\n</parameter>\n</function>\n</tool_call>\nb <function=list_capabilities>\n<parameter=server>\nx\n</parameter>\n</function>";
  const r2 = m.parseTextToolCalls(two, NAMES, SCHEMAS);
  check("two_calls_and_wrapper", r2.calls.length === 2 && r2.calls[0].args.n === 3 && r2.calls[1].name === "list_capabilities" && !r2.rest.includes("tool_call"), JSON.stringify(r2));
  const r3 = m.parseTextToolCalls("<function=demo><parameter=flag>true</parameter><parameter=obj>{\"a\":1}</parameter><parameter=s>12</parameter></function>", NAMES, SCHEMAS);
  check("typed_coercion", r3.calls[0] && r3.calls[0].args.flag === true && r3.calls[0].args.obj.a === 1 && r3.calls[0].args.s === "12", JSON.stringify(r3));
  const r4 = m.parseTextToolCalls("x <function=rm_rf>\n<parameter=p>\n/\n</parameter>\n</function>", NAMES, SCHEMAS);
  check("unknown_name_is_malformed_not_a_call", r4.calls.length === 0 && r4.malformed.length === 1, JSON.stringify(r4));
  const r5 = m.parseTextToolCalls("see:\n```\n<function=list_capabilities>\n<parameter=server>\nx\n</parameter>\n</function>\n```\n", NAMES, SCHEMAS);
  check("fenced_code_is_never_parsed", r5.calls.length === 0 && r5.malformed.length === 0 && r5.rest.includes("<function=list_capabilities>"), JSON.stringify(r5));
  const r6 = m.parseTextToolCalls("just prose, no calls", NAMES, SCHEMAS);
  check("plain_text_untouched", r6.calls.length === 0 && r6.rest === "just prose, no calls", JSON.stringify(r6));
  const r7 = m.parseTextToolCalls("<function=demo><parameter=n>notanumber</parameter></function>", NAMES, SCHEMAS);
  check("bad_number_kept_as_string", r7.calls[0] && r7.calls[0].args.n === "notanumber", JSON.stringify(r7));
}
const p = res.filter(Boolean).length;
console.log(`text_toolcalls_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`);
process.exit(res.length && p === res.length ? 0 : 1);
