// tests/probe_dsh_loopguard.mjs -- contract probe for P36 T5+T6 (DSH side of tool-guardian).
// Written by the overseer FROM THE CONTRACT before the code existed. Same fake ctx + REAL Python
// bridge + stub MCP server as tests/dsh_smoke.mjs. Run from the repo root: node tests/probe_dsh_loopguard.mjs
// Prints `probe_dsh_loopguard: N checks, N passed, M failed`.
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as plugin from 'dsh-tool-guardian'

let pass = 0, total = 0
const check = (name, cond) => { total++; if (cond) pass++; console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${name}`) }
const sleep = ms => new Promise(r => setTimeout(r, ms))
const WARN3 = '[tool-guardian: this exact call has now run 3 times with the same result.'
const WARN4 = '[tool-guardian: this exact call has now run 4 times with the same result.'
const BLOCK = '[tool-guardian: this exact call was not run again'

// ---- pure helpers ------------------------------------------------------------------
check('exports_createLoopGuard_and_callChecksLine', typeof plugin.createLoopGuard === 'function' && typeof plugin.callChecksLine === 'function')
if (typeof plugin.createLoopGuard === 'function') {
  const g = plugin.createLoopGuard()
  const seq = []
  for (let i = 0; i < 4; i++) { seq.push(g.before('k1')); seq.push(g.after('k1', 'same output')) }
  check('guard_warns_on_3rd_and_4th_identical_results', seq[1] === '' && seq[3] === '' && seq[5].startsWith(WARN3) && seq[7].startsWith(WARN4) && seq[0] === undefined && seq[6] === undefined)
  check('guard_blocks_the_5th', g.before('k1') === 'block' && g.stats.blocks === 1 && g.stats.warnings === 2)
  check('guard_block_text_is_exact', typeof g.blockText === 'function' && g.blockText(4).startsWith(BLOCK))
  const h = plugin.createLoopGuard()
  const changing = [1, 2, 3, 4, 5].map(i => { h.before('k2'); return h.after('k2', 'tick ' + i) })
  check('changing_results_never_warn', changing.every(p => p === '') && h.before('k2') === undefined)
  const iso = plugin.createLoopGuard()
  for (let i = 0; i < 3; i++) { iso.before('a'); iso.after('a', 'x') }
  check('keys_are_independent', iso.before('b') === undefined && iso.after('b', 'x') === '')
  const custom = plugin.createLoopGuard({ warnAt: 2, blockAt: 3 })
  custom.after('c', 'x')
  const second = custom.after('c', 'x')
  check('thresholds_are_configurable', second.includes('2 times') && custom.before('c') === 'block')
}
if (typeof plugin.callChecksLine === 'function') {
  check('call_checks_line_exact', plugin.callChecksLine({ schema_rejects: 2, schema_coercions: 1, loop_warnings: 3, loop_blocks: 1 }, { warnings: 1, blocks: 0 })
    === 'call checks: 2 bad calls stopped before the server, 1 fixed silently, 4 repeat warnings, 1 repeats refused')
  check('call_checks_line_zeros', plugin.callChecksLine({}) === 'call checks: 0 bad calls stopped before the server, 0 fixed silently, 0 repeat warnings, 0 repeats refused')
}
const base = plugin.Config({})
const lg = plugin.resolveOptions(base, {}).loopGuard
check('options_default_loop_guard', lg?.enabled === true && lg?.warnAt === 3 && lg?.blockAt === 5)
check('options_env_turns_it_off', plugin.resolveOptions(base, { TG_LOOP_GUARD: '0' }).loopGuard?.enabled === false)

// ---- hooks, with the real bridge ---------------------------------------------------------
const STUB = String.raw`
import json, sys
TOOLS = [{"name": "echo", "description": "Echo back the text you send.",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "stub", "version": "1"}}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        res = {"content": [{"type": "text", "text": "echo: " + str((p.get("arguments") or {}).get("text", ""))}]}
    elif mid is None:
        continue
    else:
        res = {}
    if mid is not None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
        sys.stdout.flush()
`
const tmp = mkdtempSync(join(tmpdir(), 'tg-loop-'))
writeFileSync(join(tmp, 'stub_server.py'), STUB, 'utf8')
process.env.TOOL_GUARDIAN_CALL_LOG = join(tmp, 'calls.jsonl')
process.env.TOOL_GUARDIAN_STATE = join(tmp, 'state.json')
process.env.GUARDIAN_NO_UPDATE_CHECK = '1'
delete process.env.TOOL_GUARDIAN_CONFIG
delete process.env.TG_LOOP_GUARD

const registered = new Map(); const listeners = {}; const injected = []; const effects = []; const logs = []
const logger = Object.fromEntries(['debug', 'info', 'warn', 'error'].map(l => [l, m => logs.push(`${l}: ${m}`)]))
const ctx = {
  logger,
  inject: (deps, cb) => injected.push({ deps, cb }),
  effect: fn => effects.push(fn()),
  on: (event, fn, opts) => { listeners[event] = { fn, opts } },
  tools: { register: (def) => { registered.set(def.name, def); return () => registered.delete(def.name) } },
}
const python = plugin.resolveOptions(base).python
const cfg = plugin.Config({ mcpServers: { stub: { command: python, args: [join(tmp, 'stub_server.py')] } }, spillDir: join(tmp, 'spill') })
plugin.apply(ctx, cfg)
let section = null
const settingsDep = injected.find(i => i.deps.includes('settings'))
settingsDep?.cb({ settings: { installSection: (owner, ns, schema, entry, hooks) => { section = { ns, hooks } } } })
section?.hooks.setSource(() => cfg); section?.hooks.onChange()
for (let i = 0; i < 120 && !logs.some(l => l.includes('tool-guardian ready')); i++) await sleep(250)
check('bridge_ready', logs.some(l => l.startsWith('info: tool-guardian ready:')))

const pre = listeners['tools/pre-execute']
const post = listeners['tools/post-execute']
const allow = { kind: 'allow' }
const exec = (name, args) => ({ name, callId: `c-${Math.random()}`, arguments: args })
const callOnce = async (name, args, text) => {
  const e = exec(name, args)
  const p = await pre.fn(e, async () => allow)
  if (p !== allow) return { pre: p }
  const out = await post.fn(e, { isError: false, content: [{ type: 'text', text }] }, async () => ({ kind: 'accept' }))
  return { pre: p, text: out.content?.[0]?.text ?? text }
}
const runs = []
for (let i = 0; i < 5; i++) runs.push(await callOnce('bash', { command: 'cat status.txt' }, 'status: idle'))
check('hook_3rd_and_4th_identical_bash_calls_warn', !String(runs[1].text).includes('[tool-guardian: this exact call') && String(runs[2].text).startsWith(WARN3) && String(runs[3].text).startsWith(WARN4))
check('hook_5th_identical_bash_call_is_denied', runs[4].pre?.kind === 'deny' && String(runs[4].pre?.reason).startsWith(BLOCK))
const varied = []
for (let i = 0; i < 4; i++) varied.push(await callOnce('bash', { command: 'date' }, 'time ' + i))
check('hook_changing_results_pass_untouched', varied.every(r => r.pre === allow && !String(r.text).includes('[tool-guardian: this exact call')))
const own = []
for (let i = 0; i < 5; i++) own.push(await callOnce('call_tool', { server: 'stub', tool: 'echo', args: { text: 'x' } }, 'echo: x'))
check('hook_router_tools_are_left_to_the_router', own.every(r => r.pre === allow && !String(r.text).startsWith('[tool-guardian: this exact call')))
process.env.TG_LOOP_GUARD = '0'
const off = []
for (let i = 0; i < 5; i++) off.push(await callOnce('bash', { command: 'cat other.txt' }, 'same'))
check('hook_env_off_disables_at_call_time', off.every(r => r.pre === allow && !String(r.text).includes('[tool-guardian: this exact call')))
delete process.env.TG_LOOP_GUARD

effects[0]?.()
await sleep(1500)
check('no_error_logs', !logs.some(l => l.startsWith('error:')))
console.log(`probe_dsh_loopguard: ${total} checks, ${pass} passed, ${total - pass} failed`)
process.exit(pass === total && total > 0 ? 0 : 1)
