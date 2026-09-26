// Contract probe for the 2026-09-26 MVP additions to the DSH bundle: /toolguardian, restore_groups,
// the restore offer and session-scoped bypass summaries. Written from the contract before the code.
// Run from the repo root after `pnpm install`:   node tests/probe_dsh_mvp.mjs
// A fake ctx (no DSH boot), the REAL Python bridge, and two stub MCP servers in a temp dir.
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import * as plugin from 'dsh-tool-guardian'

let pass = 0, total = 0
const check = (name, cond) => { total++; if (cond) pass++; console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${name}`) }
const sleep = ms => new Promise(r => setTimeout(r, ms))

const STUB = String.raw`
import json, sys
TOOLS = [
    {"name": "echo", "description": "Echo back the text you send.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "stub_render_video", "description": "Pretend to render.", "inputSchema": {"type": "object", "properties": {}}},
]
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
        res = {"content": [{"type": "text", "text": p.get("name", "") + ": " + str((p.get("arguments") or {}).get("text", ""))}]}
    elif mid is None:
        continue
    else:
        res = {}
    if mid is not None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
        sys.stdout.flush()
`

const tmp = mkdtempSync(join(tmpdir(), 'tg-mvp-'))
writeFileSync(join(tmp, 'stub_server.py'), STUB, 'utf8')
const logFile = join(tmp, 'calls.jsonl')
const stateFile = join(tmp, 'state.json')
process.env.TOOL_GUARDIAN_CALL_LOG = logFile
process.env.TOOL_GUARDIAN_STATE = stateFile
process.env.GUARDIAN_NO_UPDATE_CHECK = '1'
delete process.env.TOOL_GUARDIAN_CONFIG

// The previous session: it activated "stub", and one of its shell calls bypassed the router.
writeFileSync(stateFile, JSON.stringify({ version: 1, sessions: [
  { id: 'sPREV', started: '2026-09-25T20:00:00', updated: '2026-09-25T21:00:00', active_groups: ['stub'] },
] }), 'utf8')
writeFileSync(logFile, [
  { kind: 'session', event: 'start', harness: 'dsh', session: 'sPREV', ok: true, ts: '2026-09-25T20:00:00' },
  { kind: 'bypass', tool: 'bash', server: 'stub', target: 'prev_only_target', mode: 'nudge', ok: true, session: 'sPREV', ts: '2026-09-25T20:30:00' },
].map(r => JSON.stringify(r)).join('\n') + '\n', 'utf8')

const registered = new Map(); const listeners = {}; const injected = []; const effects = []; const logs = []
const logger = Object.fromEntries(['debug', 'info', 'warn', 'error'].map(l => [l, m => logs.push(`${l}: ${m}`)]))
const ctx = {
  logger,
  inject: (deps, cb) => injected.push({ deps, cb }),
  effect: fn => effects.push(fn()),
  on: (event, fn, opts) => { listeners[event] = { fn, opts } },
  tools: { register: (def) => { if (registered.has(def.name)) throw new Error(`duplicate ${def.name}`); registered.set(def.name, def); return () => registered.delete(def.name) } },
}
process.chdir(tmpdir())
const python = plugin.resolveOptions(plugin.Config({})).python
const stub = { command: python, args: [join(tmp, 'stub_server.py')] }
plugin.apply(ctx, plugin.Config({ mcpServers: { stub, stub2: stub }, spillDir: join(tmp, 'spill') }))

// 1. the command is registered through the commands service, with an input hint
const commandsInject = injected.find(i => i.deps.includes('commands'))
const commands = new Map()
commandsInject?.cb({ commands: { register: (def) => { commands.set(def.name, def); return () => commands.delete(def.name) } } })
const tgCommand = commands.get('toolguardian')
check('toolguardian_command_registered_with_input_hint', tgCommand !== undefined && typeof tgCommand.handler === 'function'
  && typeof tgCommand.input?.hint === 'string' && tgCommand.input.hint.includes('bypass') && /^[a-z][a-z0-9_-]*$/.test(tgCommand.name))
const settingsInject = injected.find(i => i.deps.includes('settings'))
check('settings_inject_still_present', settingsInject !== undefined)
const run = raw => (tgCommand?.handler ?? (async () => ({ kind: 'missing', text: '' })))({ rawInput: raw, agent: {}, attachments: [], signal: new AbortController().signal })

// 2. before the backends report, the command answers instead of throwing
const early = await run('')
check('command_before_ready_is_a_clear_error_not_a_throw', early.kind === 'error' && /starting|not ready/.test(early.text))

for (let i = 0; i < 120 && !logs.some(l => l.includes('tool-guardian ready')); i++) await sleep(250)
for (let i = 0; i < 20 && !logs.some(l => l.includes('last session used')); i++) await sleep(250)

// 3. offered, never forced
check('restore_offer_logged_not_loaded', logs.some(l => l.includes('last session used groups stub') && l.includes('restore_groups'))
  && !registered.has('tg__stub__echo'))
check('restore_groups_tool_registered', registered.has('restore_groups') && registered.get('restore_groups').parameters?.type === 'object')
const groupsText = await registered.get('list_groups_with_costs')?.execute({}) ?? ''
check('list_groups_names_the_restore_offer', groupsText.includes('Last session used: stub') && groupsText.includes('restore_groups()')
  && groupsText.trimEnd().split('\n').at(-1).startsWith('NEXT STEP:'))

// 4. the selftest report
const report = await run('')
const alias = await run(' selftest ')
check('selftest_reports_backends_saving_and_update', report.kind === 'success'
  && /stub\s+ok\s+2 tools/.test(report.text) && /stub2\s+ok\s+2 tools/.test(report.text)
  && /backends: 2 up, 0 down/.test(report.text) && /tokens freed on every request|router saves little/.test(report.text)
  && report.text.includes('update: check off') && report.text.includes('/toolguardian bypass'))
check('selftest_names_the_restore_offer', report.text.includes('last session used: stub'))
check('selftest_alias_and_whitespace', alias.kind === 'success' && alias.text.split('\n')[0] === report.text.split('\n')[0])
check('selftest_does_not_claim_the_model_uses_the_router', /does NOT prove your model/i.test(report.text))

// 5. restore loads exactly the offered groups, and records them for THIS session
const restored = await registered.get('restore_groups')?.execute({}) ?? ''
check('restore_groups_activates_the_offer', registered.has('tg__stub__echo') && !registered.has('tg__stub2__echo')
  && restored.includes('loaded: stub') && /~\d+ tokens added/.test(restored))
const again = await run('restore')
check('restore_twice_is_a_noop', again.kind === 'success' && again.text.includes('already active') && !again.text.includes('loaded:'))
const act = await registered.get('activate_group').execute({ group: 'stub2' })
check('activate_group_still_works', act.includes('group "stub2" is active'))
await sleep(500)
let state = { sessions: [] }
try { state = JSON.parse(readFileSync(stateFile, 'utf8')) } catch { /* red run */ }
state.sessions ??= []
const mine = state.sessions.filter(s => s.id !== 'sPREV')
check('activations_recorded_for_the_current_session', mine.length === 1 && mine[0].active_groups.join() === 'stub,stub2'
  && state.sessions.some(s => s.id === 'sPREV' && s.active_groups.join() === 'stub'))
check('catalogue_recorded_for_the_claude_code_hook', state.catalogue?.servers?.stub?.includes('stub_render_video'))

// 6. bypass summary: current session, previous session
const pre = listeners['tools/pre-execute']
await pre.fn({ name: 'bash', callId: 'b1', arguments: { command: 'python run.py stub_render_video --x' } }, async () => ({ kind: 'allow' }))
await sleep(500)
const bypass = await run('bypass')
check('bypass_summary_current_session', bypass.kind === 'success' && bypass.text.includes('1 bypass')
  && bypass.text.includes('call_tool(server="stub", tool="stub_render_video"') && !bypass.text.includes('prev_only_target'))
const last = await run('bypass last')
check('bypass_summary_previous_session', last.kind === 'success' && last.text.includes('prev_only_target') && !last.text.includes('stub_render_video'))
const rows = readFileSync(logFile, 'utf8').trim().split('\n').map(l => JSON.parse(l))
const current = rows.find(r => r.kind === 'session' && r.harness === 'dsh' && r.session !== 'sPREV')
check('session_start_row_and_every_new_row_carries_the_session', current !== undefined
  && rows.filter(r => r.ts > '2026-09-26' || r.session !== 'sPREV').filter(r => r.session !== 'sPREV').every(r => r.session === current.session))

// 7. bad input
const bad = await run('frobnicate')
check('unknown_subcommand_is_an_error_with_usage', bad.kind === 'error' && bad.text.includes('usage: /toolguardian'))

effects[0]()
await sleep(1500)
check('unload_unregisters_everything', registered.size === 0)
check('no_error_logs', !logs.some(l => l.startsWith('error:')))
if (logs.some(l => l.startsWith('error:') || l.startsWith('warn:'))) console.log(logs.filter(l => !l.startsWith('debug')).join('\n'))
console.log(`probe_dsh_mvp: ${total} checks, ${pass} passed, ${total - pass} failed`)
process.exit(pass === total ? 0 : 1)
