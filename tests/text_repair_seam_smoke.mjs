// P45 B2 seam smoke (written 2026-10-03, before text_toolcall_repair.js existed -- red first).
// Fake ctx, no DSH boot, no Python bridge (eagerStart false): proves index.js wires the C-Repair
// transform onto `llm/stream`, honours enabled / logOnly / the env switch, and logs one line per call.
//   node tests/text_repair_seam_smoke.mjs
import { mkdtempSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
const here = dirname(fileURLToPath(import.meta.url))
const res = []
const check = (n, c, why = '') => { res.push(Boolean(c)); console.log((c ? 'ok   ' : 'FAIL ') + n + (c || !why ? '' : '  -- ' + why)) }
let plugin = null
try { plugin = await import(pathToFileURL(join(here, '..', 'index.js')).href) } catch (e) { check('index_loads', false, String(e && e.message)) }
const T1 = readFileSync(join(here, 'fixtures', 'textcalls', 'tg2_B_T1_r1_s71.txt'), 'utf8')
const tools = [{ name: 'search_capabilities', description: '', parameters: { type: 'object', properties: { query: { type: 'string' }, server: { type: 'string' } } } }]
function chunks(text) {
  const out = [{ type: 'block-start', index: 0, blockType: 'text' }]
  for (let i = 0; i < text.length; i += 9) out.push({ type: 'text-delta', index: 0, text: text.slice(i, i + 9) })
  return out.concat([{ type: 'block-end', index: 0, block: { type: 'text', text } }, { type: 'usage', usage: { inputTokens: 1, outputTokens: 1 } }, { type: 'finish', reason: { kind: 'stop' } }])
}
async function* gen(list) { for (const c of list) yield c }
async function drain(it) { const o = []; for await (const c of it) o.push(c); return o }
function boot(cfg) {
  const listeners = {}
  const ctx = {
    on: (name, fn) => { (listeners[name] ??= []).push(fn); return () => {} },
    inject: () => {}, effect: () => {}, command: () => {},
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    tools: { register: () => () => {} },
  }
  plugin.apply(ctx, plugin.Config(cfg))
  return listeners['llm/stream'] || []
}
if (plugin) {
  const dir = mkdtempSync(join(tmpdir(), 'tg-repair-'))
  const logPath = join(dir, 'calls.jsonl')
  process.env.TOOL_GUARDIAN_CALL_LOG = logPath
  delete process.env.TOOL_GUARDIAN_TEXTCALL_REPAIR
  const hooks = boot({ eagerStart: false })
  check('one_llm_stream_listener', hooks.length === 1, String(hooks.length))
  const h = hooks[0]
  if (h) {
    const out = await drain(h({ tools, sessionId: 's1' }, () => gen(chunks(T1))))
    const call = out.find((c) => c.type === 'block-end' && c.block.type === 'tool-call')
    check('text_call_repaired_to_tool_call', Boolean(call) && call.block.name === 'search_capabilities' && JSON.parse(call.block.arguments).query === 'restore_head')
    check('finish_is_tool_calls', out[out.length - 1].reason.kind === 'tool-calls')
    const lines = existsSync(logPath) ? readFileSync(logPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : []
    check('one_log_line_repaired', lines.length === 1 && lines[0].event === 'text_toolcall_repaired' && lines[0].tool === 'search_capabilities' && lines[0].session === 's1', JSON.stringify(lines))
    const plain = chunks('just prose')
    check('plain_text_identical', JSON.stringify(await drain(h({ tools, sessionId: 's1' }, () => gen(plain)))) === JSON.stringify(plain))
    const sentinel = gen(chunks(T1))
    check('no_tools_returns_next_untouched', h({ tools: [] }, () => sentinel) === sentinel)
    process.env.TOOL_GUARDIAN_TEXTCALL_REPAIR = '0'
    const s2 = gen(chunks(T1))
    check('env_off_returns_next_untouched', h({ tools }, () => s2) === s2)
    delete process.env.TOOL_GUARDIAN_TEXTCALL_REPAIR
  }
  const lo = boot({ eagerStart: false, textToolCallRepair: { logOnly: true } })[0]
  if (lo) {
    const src = chunks(T1)
    const out = await drain(lo({ tools, sessionId: 's2' }, () => gen(src)))
    const lines = readFileSync(logPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l))
    check('log_only_changes_nothing_but_logs', JSON.stringify(out) === JSON.stringify(src) && lines[lines.length - 1].event === 'text_toolcall_seen' && lines[lines.length - 1].session === 's2')
  }
  const off = boot({ eagerStart: false, textToolCallRepair: { enabled: false } })[0]
  if (off) { const s3 = gen(chunks(T1)); check('config_off_returns_next_untouched', off({ tools }, () => s3) === s3) }
  const thrower = boot({ eagerStart: false })[0]
  if (thrower) { const s4 = gen([]); check('bad_options_fail_open', thrower(null, () => s4) === s4) }
}
const p = res.filter(Boolean).length
console.log(`text_repair_seam_smoke: ${res.length} checks, ${p} passed, ${res.length - p} failed`)
process.exit(res.length && p === res.length ? 0 : 1)
