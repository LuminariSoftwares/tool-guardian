// DSH-side smoke for the bundle entry. Run from the repo root after `pnpm install`:
//   node tests/dsh_smoke.mjs
// A fake ctx (no DSH boot), the REAL Python bridge, and a stub MCP server written to a temp dir.
// It proves the plugin's own logic; it does NOT prove DSH accepts the registrations -- that is the live boot.
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
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

const tmp = mkdtempSync(join(tmpdir(), 'tg-smoke-'))
writeFileSync(join(tmp, 'stub_server.py'), STUB, 'utf8')
process.env.TOOL_GUARDIAN_CALL_LOG = join(tmp, 'calls.jsonl')
delete process.env.TOOL_GUARDIAN_CONFIG

const base = plugin.Config({})
check('name_and_inject', plugin.name === 'dsh-tool-guardian' && plugin.inject.join() === 'tools')
check('schema_defaults', base.eagerStart === true && base.ladder.enabled === true && base.bypass.mode === 'nudge' && base.activeGroups.length === 0)
check('env_beats_section', plugin.resolveOptions({ ...base, configPath: 'a.json' }, { TOOL_GUARDIAN_CONFIG: 'b.json', TOOL_GUARDIAN_CALL_TIMEOUT: '7', TOOL_GUARDIAN_LADDER: '0' }).configPath === 'b.json'
  && plugin.resolveOptions(base, { TOOL_GUARDIAN_CALL_TIMEOUT: '7' }).callTimeoutMs === 7000
  && plugin.resolveOptions(base, { TOOL_GUARDIAN_LADDER: '0' }).ladder.enabled === false)
check('direct_tool_name_is_a_legal_function_name', /^[A-Za-z0-9_-]{1,64}$/.test(plugin.directToolName('my.server', 'x'.repeat(80))))
check('flatten_text_refuses_non_text_blocks', plugin.flattenText([{ type: 'text', text: 'a' }, { type: 'image' }]) === undefined
  && plugin.flattenText([{ type: 'text', text: 'a' }, { type: 'text', text: 'b' }]) === 'a\nb')
const matcher = plugin.buildBypassMatcher({ rules: [{ pattern: 'localhost:5679', server: 'n8n', tool: 'n8n_list_workflows' }, { pattern: '([', server: 'x', tool: 'y' }], minNameLength: 8 },
  [{ tools: ['stub.echo', 'stub.stub_render_video'] }])
check('bypass_matcher', matcher('curl http://localhost:5679/api')?.tool === 'n8n_list_workflows'
  && matcher('python run.py stub_render_video --x')?.server === 'stub'
  && matcher('echo hello') === undefined && matcher('xstub_render_videox') === undefined && matcher(undefined) === undefined)

// restrictKnown: learn an agent's tool set from the registry's own refusal
const fakeTools = (known) => {
  const state = { calls: [], disposed: 0 }
  state.restrict = ({ deny }) => {
    state.calls.push([...deny])
    const unknown = deny.filter(n => !known.includes(n))
    if (unknown.length > 0) throw new Error(`tools.restrict() names unknown global tools ${unknown.map(n => `"${n}"`).join(', ')}; known global tools: ${[...known].sort().join(', ')}`)
    return () => { state.disposed++ }
  }
  return state
}
const probeTools = fakeTools(['subagent', 'workflow', 'create_goal', 'pwsh'])
const firstTry = plugin.restrictKnown(probeTools, ['subagent', 'ralph', 'workflow'])
check('restrict_known_retries_with_the_names_the_registry_knows', firstTry.denied.join() === 'subagent,workflow' && probeTools.calls.length === 2 && typeof firstTry.dispose === 'function')
check('restrict_known_with_nothing_present_restricts_nothing', plugin.restrictKnown(fakeTools(['pwsh']), ['ralph']).dispose === null && plugin.restrictKnown(fakeTools(['pwsh']), []).denied.length === 0)
let rethrown = false
try { plugin.restrictKnown({ restrict: () => { throw new Error('requires a scoped context') } }, ['x']) } catch { rethrown = true }
check('restrict_known_rethrows_any_other_refusal', rethrown)
check('builtin_groups_are_off_by_default', base.builtinGroups.enabled === false && base.builtinGroups.hidden.length === 0 && base.builtinGroups.groups.delegation.includes('workflow'))

// fake ctx: records what apply() asks for
const registered = new Map(); const listeners = {}; const injected = []; const effects = []; const logs = []
const logger = Object.fromEntries(['debug', 'info', 'warn', 'error'].map(l => [l, m => logs.push(`${l}: ${m}`)]))
const ctx = {
  logger,
  inject: (deps, cb) => injected.push({ deps, cb }),
  effect: fn => effects.push(fn()),
  on: (event, fn, opts) => { listeners[event] = { fn, opts } },
  tools: { register: (def) => { if (registered.has(def.name)) throw new Error(`duplicate ${def.name}`); registered.set(def.name, def); return () => registered.delete(def.name) } },
}
process.chdir(tmpdir())                                      // apply() must not care about the CWD
const python = plugin.resolveOptions(base).python
const cfg = plugin.Config({
  mcpServers: { stub: { command: python, args: [join(tmp, 'stub_server.py')] } },
  spillDir: join(tmp, 'spill'),
  builtinGroups: { enabled: true, hidden: ['delegation', 'goals', 'not-a-group'], groups: { delegation: ['subagent', 'workflow', 'ralph'], goals: ['create_goal'], jobs: ['job_list'] } },
})
plugin.apply(ctx, cfg)
check('activate_group_exists_before_the_backends_report', registered.has('activate_group') && registered.get('activate_group').description.includes('Hidden built-in groups: delegation (subagent, workflow, ralph); goals (create_goal)'))
let section = null
injected[0].cb({ settings: { installSection: (owner, ns, schema, entry, hooks) => { section = { ns, hooks } } } })
section.hooks.setSource(() => cfg); section.hooks.onChange()  // provider attaches with identical options: must NOT restart
for (let i = 0; i < 120 && !logs.some(l => l.includes('tool-guardian ready')); i++) await sleep(250)
check('ready_line_logged', logs.some(l => l.startsWith('info: tool-guardian ready:')))
check('backend_reported', logs.some(l => l.includes('stub=ok(2)')))
const names = [...registered.keys()]
check('router_tools_registered_after_backends', ['list_capabilities', 'describe_tool', 'call_tool', 'list_groups_with_costs', 'retrieve_spill', 'activate_group'].every(n => names.includes(n)))
check('backend_tools_are_NOT_registered_until_a_group_is_active', !names.some(n => n.startsWith('tg__')))
check('definitions_have_the_registry_shape', [...registered.values()].every(d => typeof d.execute === 'function' && typeof d.output?.render === 'function' && d.output.schema.type === 'string' && d.parameters?.type === 'object'))

const callTool = registered.get('call_tool')
const small = await callTool.execute({ server: 'stub', tool: 'echo', args: { text: 'hi' } })
check('call_tool_roundtrip_with_next_step', small.startsWith('echo: hi') && small.includes('NEXT STEP:'))
const groupsText = await registered.get('list_groups_with_costs').execute({})
check('groups_priced_and_point_at_activate_group', /stub\s+2 tools\s+~\d+ tokens/.test(groupsText) && groupsText.includes('activate_group(group='))

// the ladder on ANY tool's result (the dsh-trim role)
const post = listeners['tools/post-execute']
const big = Array.from({ length: 3000 }, (_, i) => `line ${String(i + 1).padStart(5, '0')} output text`).join('\n') + '\n[exit code: 0]\n'
const run = (name, text, extra = {}) => {
  const result = { isError: false, content: [{ type: 'text', text }], ...extra }
  const decision = { kind: 'accept' }
  return post.fn({ name, callId: `c-${Math.random()}`, arguments: {} }, result, async () => decision).then(out => ({ out, decision }))
}
check('post_execute_is_prepended', post?.opts?.prepend === true)
const shaped = await run('bash', big)
const shapedText = shaped.out.content?.[0]?.text ?? ''
const spillId = /sp_[0-9a-f]{12}/.exec(shapedText)?.[0]
check('big_bash_result_is_shaped_and_archived', shaped.out.kind === 'accept' && shapedText.length < 9000 && shapedText.includes('[tool-guardian: showing') && shapedText.includes('[exit code: 0]') && spillId !== undefined)
const back = await registered.get('retrieve_spill').execute({ id: spillId, grep: 'line 01500 ' })
check('archived_original_is_recoverable', back.includes('L1500: line 01500 output text') && existsSync(join(tmp, 'spill', `${spillId}.txt`)))
const harnessNotice = '(Omitted 4824 bytes. Full formatted result stored at: F:\\AI\\.tmp\\dsh-spill-X\\session-1\\abc-pwsh.txt. Use read with offset/limit, or grep this path to search within it.)'
const spilled = await run('pwsh', `${big}\n\n${harnessNotice}`)
const spilledText = spilled.out.content?.[0]?.text ?? ''
check('harness_spilled_preview_is_shaped_and_keeps_the_harness_locator', spilledText.length < 9000 && spilledText.trimEnd().endsWith(harnessNotice) && !spilledText.includes('[tool-guardian: showing') && spilledText.includes('lines omitted'))
const tiny = await run('bash', 'ok\n')
check('small_result_returns_the_same_decision_object', tiny.out === tiny.decision)
const skipped = await run('read', big)
check('read_is_exempt', skipped.out === skipped.decision)
const own = await run('call_tool', big)
check('own_tools_are_not_shaped_twice', own.out === own.decision)
const image = await post.fn({ name: 'bash', callId: 'img', arguments: {} }, { isError: false, content: [{ type: 'image', data: 'x' }] }, async () => ({ kind: 'accept' }))
check('non_text_results_are_never_touched', image.content === undefined)
const blocked = { kind: 'block', feedback: [] }
check('non_accept_decisions_pass_through', await post.fn({ name: 'bash', callId: 'b', arguments: {} }, { isError: false, content: [{ type: 'text', text: big }] }, async () => blocked) === blocked)

// bypass: nudge mode = allow, log, and append the exact router call to THAT call's result
const pre = listeners['tools/pre-execute']
const allow = { kind: 'allow' }
const preOut = await pre.fn({ name: 'bash', callId: 'byp-1', arguments: { command: 'python jobs.py stub_render_video --fast' } }, async () => allow)
const nudged = await post.fn({ name: 'bash', callId: 'byp-1', arguments: {} }, { isError: false, content: [{ type: 'text', text: 'done' }] }, async () => ({ kind: 'accept' }))
check('bypass_nudge_allows_then_names_the_exact_call', preOut === allow && (nudged.content?.[0]?.text ?? '').includes('call_tool(server="stub", tool="stub_render_video"'))
const clean = await pre.fn({ name: 'bash', callId: 'ok-1', arguments: { command: 'git status' } }, async () => allow)
check('ordinary_shell_is_left_alone', clean === allow)
await sleep(300)
const logRows = readFileSync(join(tmp, 'calls.jsonl'), 'utf8').trim().split('\n').map(l => JSON.parse(l))
check('shaping_a_harness_preview_archives_nothing_twice', logRows.some(r => r.kind === 'ladder' && r.tool === 'pwsh' && r.spill_id === '' && r.final_chars < r.original_chars))
check('router_calls_ladder_and_bypass_are_all_logged', logRows.some(r => r.kind === 'router' && r.tool === 'call_tool') && logRows.some(r => r.kind === 'ladder' && r.tool === 'bash' && r.spill_id === spillId) && logRows.some(r => r.kind === 'bypass' && r.target === 'stub_render_video' && r.mode === 'nudge'))

// built-in groups: hidden per agent at creation, restored per agent by activate_group
const agentTools = fakeTools(['subagent', 'workflow', 'create_goal', 'job_list', 'pwsh', 'call_tool'])
const agent = { ctx: { tools: agentTools } }
listeners['agent/created'].fn({ agent })
check('new_agent_gets_hidden_groups_denied', agentTools.calls.at(-1).join() === 'subagent,workflow,create_goal')
const withBuiltins = await registered.get('list_groups_with_costs').execute({}, { agent })
check('manifest_lists_builtin_groups_with_the_exact_call', withBuiltins.includes('delegation  3 tools  HIDDEN -- activate_group(group="delegation")') && withBuiltins.includes('jobs  1 tools  active') && withBuiltins.trimEnd().split('\n').at(-1).startsWith('NEXT STEP:'))
const lifted = await registered.get('activate_group').execute({ group: 'delegation' }, { agent })
check('activate_builtin_group_lifts_only_that_group', lifted.includes('is active for this session') && agentTools.disposed === 1 && agentTools.calls.at(-1).join() === 'create_goal')
const again = await registered.get('activate_group').execute({ group: 'delegation' }, { agent })
check('activating_twice_is_a_noop_with_next_step', again.includes('already active') && agentTools.disposed === 1)
const other = { ctx: { tools: fakeTools(['pwsh']) } }
listeners['agent/created'].fn({ agent: other })
check('agent_without_those_tools_is_left_alone', other.ctx.tools.calls.length === 1 && other.ctx.tools.disposed === 0)
listeners['agent/disposed'].fn({ agent })
check('disposed_agent_mask_is_lifted', agentTools.disposed === 2)

// groups: direct tools exist only once a group is active
const act = await registered.get('activate_group').execute({ group: 'stub' })
check('activate_group_registers_direct_tools_and_quotes_cost', registered.has('tg__stub__echo') && registered.has('tg__stub__stub_render_video') && /~\d+ tokens added/.test(act))
const direct = await registered.get('tg__stub__echo').execute({ text: 'yo' })
check('direct_tool_routes_through_the_router', direct.startsWith('echo: yo'))
const unknown = await registered.get('activate_group').execute({ group: 'nope' })
check('unknown_group_is_a_result_with_next_step_not_a_throw', unknown.includes('activate_group failed') && unknown.includes('NEXT STEP: list_groups_with_costs()'))

effects[0]()                                                  // plugin unload
await sleep(1500)
check('unload_unregisters_everything', registered.size === 0)
check('no_error_logs', !logs.some(l => l.startsWith('error:')))
if (logs.some(l => l.startsWith('error:') || l.startsWith('warn:'))) console.log(logs.filter(l => !l.startsWith('debug')).join('\n'))
console.log(`dsh-tool-guardian smoke: ${total} checks, ${pass} passed, ${total - pass} failed`)
process.exit(pass === total ? 0 : 1)
