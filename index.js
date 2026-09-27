/**
 * dsh-tool-guardian -- Tool Guardian as a native DSH bundle plugin.
 *
 * What it does inside DSH:
 *   - registers the router tools NATIVELY (list_capabilities, describe_tool,
 *     call_tool, list_groups_with_costs, retrieve_spill, activate_group) -- the
 *     MCP backends behind them are never registered with DSH, so their schemas
 *     never enter a request unless their group is active;
 *   - runs EVERY tool's result (bash, grep, web_fetch, ...) down the output
 *     ladder on `tools/post-execute`, before the model ever sees it, archiving the
 *     full original behind anything lossy (this replaces the dsh-trim plugin;
 *     trim's design -- next() first, fail open, spill before anything lossy --
 *     is followed here, credit: shuistama/dsh-trim, MIT);
 *   - watches shell tools on `tools/pre-execute` for calls that bypass the
 *     router, and logs / nudges / denies them;
 *   - remembers which groups a session activated (~/.tool-guardian/state.json)
 *     and OFFERS them to the next session through `restore_groups` -- never
 *     activates them by itself;
 *   - adds the `/toolguardian` command: a selftest in the conversation (backends,
 *     token saving, update line), `/toolguardian bypass` and `/toolguardian restore`.
 *
 * A bridge, not a rewrite: routing stays in tool_guardian.py. This plugin
 * spawns modules/tg_bridge.py (a private NDJSON protocol, NOT MCP), which
 * imports tool_guardian.Router unchanged. The MCP deployment keeps working.
 *
 * Written against DSH 0.1.2-alpha.2 (source read 2026-09-19):
 *   - tool registry:  packages/core/tools        -> ctx.tools.register(defineTool({...}))
 *   - settings:       packages/settings/settings -> ctx.settings.installSection(owner, ns, schema, entry, hooks)
 *   - commands:       packages/interaction/commands -> ctx.commands.register({ name, description, input, handler })
 *                     (handler returns { kind: 'success' | 'error', text }; without `input` DSH treats the
 *                     command as argument-free and sends "/toolguardian bypass" to the MODEL)
 *                     (the removed installSettingsSection/settingsNamespace helpers are NOT used)
 *   - bundle format:  docs/user/develop/basic/publish.md
 *
 * MIT licensed.
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import Schema from '@deepseek-ai/schemastery'
import { checkForUpdate, defaultCacheFile } from './update_check.js'

export const name = 'dsh-tool-guardian'

/** Cordis waits for the native tool registry before apply() runs. */
export const inject = ['tools']

/** Settings namespace (must match /^[a-z][a-z0-9-]*$/). */
export const SETTINGS_NAMESPACE = 'tool-guardian'

/**
 * This package's own directory, resolved from the module URL -- never from
 * process.cwd(). DSH is launched from the dsh checkout, from a job workspace,
 * or from a profile directory; none of them is where this file lives.
 */
const PACKAGE_ROOT = dirname(fileURLToPath(import.meta.url))
const BRIDGE_SCRIPT = join(PACKAGE_ROOT, 'modules', 'tg_bridge.py')

/** This plugin's own version, from the package.json beside this file. */
const PKG_VERSION = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version

export const Config = Schema.object({
  python: Schema.string().default('')
    .description('Python interpreter for the bridge. Empty = $TOOL_GUARDIAN_PYTHON, then a .venv beside this package, then python/python3 on PATH.'),
  configPath: Schema.string().default('')
    .description('mcpServers JSON file. Empty = $TOOL_GUARDIAN_CONFIG, then tool_guardian.py\'s own search order.'),
  mcpServers: Schema.dict(Schema.any()).default({})
    .description('Inline mcpServers block (same shape as the JSON file). Non-empty wins over configPath.'),
  eagerStart: Schema.boolean().default(true)
    .description('Start the bridge and backends at plugin load. Router tools are advertised only once backends report, so false means no tools until something else starts it.'),
  startTimeoutMs: Schema.natural().default(90000)
    .description('Per-backend MCP startup budget (TOOL_GUARDIAN_START_TIMEOUT, seconds, overrides).'),
  callTimeoutMs: Schema.natural().default(120000)
    .description('Per-call budget (TOOL_GUARDIAN_CALL_TIMEOUT, seconds, overrides).'),
  skillsDirs: Schema.array(Schema.string()).default([])
    .description('Skill roots for list_skills/read_skill (TOOL_GUARDIAN_SKILLS overrides).'),
  logPath: Schema.string().default('')
    .description('Router log file (TOOL_GUARDIAN_LOG overrides).'),
  ladder: Schema.object({
    enabled: Schema.boolean().default(true)
      .description('Shape tool results before the model sees them (TOOL_GUARDIAN_LADDER=0 overrides).'),
    allTools: Schema.boolean().default(true)
      .description('Shape every DSH tool\'s result, not only the router\'s own (the dsh-trim role).'),
    skipTools: Schema.array(Schema.string()).default(['read', 'read_image', 'skill', 'todo_write'])
      .description('Tools whose results are never touched.'),
    options: Schema.dict(Schema.any()).default({})
      .description('Overrides for tg_ladder.DEFAULTS (compact_above_chars, shell_min_chars, ...).'),
  }).default({}),
  groups: Schema.dict(Schema.array(Schema.string())).default({})
    .description('Named tool groups: { group: ["server", "server.tool", "server.prefix*"] }. Empty = one group per server.'),
  activeGroups: Schema.array(Schema.string()).default([])
    .description('Groups whose tools are registered as direct native tools at load.'),
  allowRuntimeActivation: Schema.boolean().default(true)
    .description('Expose activate_group so the agent can load one group mid-session.'),
  bypass: Schema.object({
    mode: Schema.union(['off', 'log', 'nudge', 'deny']).default('nudge')
      .description('What to do when a shell tool is used for something a router tool does.'),
    shellTools: Schema.array(Schema.string()).default(['bash', 'pwsh', 'run_code']),
    rules: Schema.array(Schema.object({
      pattern: Schema.string().required(),
      server: Schema.string().required(),
      tool: Schema.string().required(),
    })).default([]).description('Extra regex -> router-tool mappings; catalogue tool names are matched automatically.'),
    minNameLength: Schema.natural().default(8)
      .description('Catalogue tool names shorter than this are not auto-matched (too noisy).'),
  }).default({}),
  loopGuard: Schema.object({
    enabled: Schema.boolean().default(true)
      .description('Guard DSH\'s OWN tools (bash, grep, web_fetch, ...) against a call repeated with the same arguments and the same result (TG_LOOP_GUARD=0 overrides).'),
    warnAt: Schema.natural().default(3)
      .description('How many identical results in a row before a repeat-call warning is prepended to the result.'),
    blockAt: Schema.natural().default(5)
      .description('How many identical results in a row before the next such call is refused before it runs.'),
  }).default({}),
  builtinGroups: Schema.object({
    enabled: Schema.boolean().default(false)
      .description('Hide groups of DSH\'s OWN tools (subagents, workflow, goals, ...) from each agent until activate_group loads them.'),
    groups: Schema.dict(Schema.array(Schema.string())).default({
      delegation: ['subagent', 'subagent_fork', 'list_agents', 'send_message', 'interrupt_agent', 'workflow', 'ralph'],
      goals: ['create_goal', 'get_goal', 'update_goal'],
      jobs: ['job_list', 'job_output', 'job_kill'],
      planning: ['exit_plan_mode', 'todo_write', 'ask_user_question'],
    }).description('Group name -> DSH tool names. Names a session does not have are ignored.'),
    hidden: Schema.array(Schema.string()).default([])
      .description('Groups hidden from every new agent. A hidden tool\'s schema is not sent until its group is activated.'),
  }).default({}),
  spillDir: Schema.string().default('')
    .description('Archive of full originals (TOOL_GUARDIAN_SPILL_DIR overrides; default ~/.tool-guardian/spill).'),
  spillKeep: Schema.natural().default(500),
})

/**
 * Fold the environment over a resolved section. The TOOL_GUARDIAN_* names are
 * the ones tool_guardian.py and the existing .bat launchers already use, so
 * an env var set for the MCP deployment means the same thing here.
 * Precedence: env > settings user layer > patch-row config > schema default.
 */
export function resolveOptions(section, env = process.env) {
  const seconds = (key, fallbackMs) => {
    const raw = Number(env[key])
    return Number.isFinite(raw) && raw > 0 ? Math.round(raw * 1000) : fallbackMs
  }
  return {
    python: env.TOOL_GUARDIAN_PYTHON?.trim() || section.python || defaultPython(),
    configPath: env.TOOL_GUARDIAN_CONFIG?.trim() || section.configPath || '',
    mcpServers: section.mcpServers ?? {},
    eagerStart: section.eagerStart !== false,
    startTimeoutMs: seconds('TOOL_GUARDIAN_START_TIMEOUT', section.startTimeoutMs),
    callTimeoutMs: seconds('TOOL_GUARDIAN_CALL_TIMEOUT', section.callTimeoutMs),
    skillsDirs: section.skillsDirs ?? [],
    logPath: env.TOOL_GUARDIAN_LOG?.trim() || section.logPath || '',
    ladder: {
      enabled: !['0', 'false', 'False', 'off', 'no'].includes(env.TOOL_GUARDIAN_LADDER ?? '') && section.ladder?.enabled !== false,
      allTools: section.ladder?.allTools !== false,
      skipTools: [...(section.ladder?.skipTools ?? ['read', 'read_image', 'skill', 'todo_write'])],
      options: { ...(section.ladder?.options ?? {}) },
    },
    groups: { ...(section.groups ?? {}) },
    activeGroups: [...(section.activeGroups ?? [])],
    allowRuntimeActivation: section.allowRuntimeActivation !== false,
    bypass: {
      mode: section.bypass?.mode ?? 'nudge',
      shellTools: [...(section.bypass?.shellTools ?? ['bash', 'pwsh', 'run_code'])],
      rules: [...(section.bypass?.rules ?? [])],
      minNameLength: section.bypass?.minNameLength ?? 8,
    },
    loopGuard: {
      enabled: section.loopGuard?.enabled !== false
        && !LOOP_OFF.has(String(env.TG_LOOP_GUARD ?? '').trim().toLowerCase()),
      warnAt: section.loopGuard?.warnAt ?? 3,
      blockAt: section.loopGuard?.blockAt ?? 5,
    },
    builtinGroups: {
      enabled: section.builtinGroups?.enabled === true
        && !['0', 'false', 'False', 'off', 'no'].includes(env.TOOL_GUARDIAN_BUILTIN_GROUPS ?? ''),
      groups: Object.fromEntries(Object.entries(section.builtinGroups?.groups ?? {}).map(([k, v]) => [k, [...v]])),
      hidden: [...(section.builtinGroups?.hidden ?? [])],
    },
    spillDir: env.TOOL_GUARDIAN_SPILL_DIR?.trim() || section.spillDir || '',
    spillKeep: section.spillKeep ?? 500,
  }
}

// ── T6: the router's repeated-call guard, for DSH's OWN tools ──────────────────
// The Python router already refuses a call_tool that keeps answering the same way.
// Nothing guards bash, grep, web_fetch and the rest -- they never pass through it --
// so an agent that re-runs one identical command pays for the same bytes until the
// context window fills. Same thresholds and the same two strings as tool_guardian.py's
// call_tool guard, so a refusal reads identically on either side of the bridge.

/** $TG_LOOP_GUARD values that switch the guard off, in any case. */
const LOOP_OFF = new Set(['0', 'false', 'off', 'no'])

/** JSON with object keys in sorted order, so argument order cannot fake a different call. */
function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${stableJson(value[k])}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/**
 * One repeated-call guard, shared by every tool of one plugin instance.
 * `before` is asked before a call runs, `after` once its result is known.
 * Pure: no clock, no I/O, so the counts it reports are its own alone.
 * @returns {{ before: (key) => 'block'|undefined, after: (key, text) => string, blockText: (n) => string, stats: { warnings: number, blocks: number } }}
 */
export function createLoopGuard({ warnAt = 3, blockAt = 5, window = 40 } = {}) {
  const recent = []   // { key, hash }, oldest first; the last `window` entries only
  const stats = { warnings: 0, blocks: 0 }
  const span = Math.max(1, blockAt - 1)   // identical results that make the next call pointless
  const warnText = (n) => `[tool-guardian: this exact call has now run ${n} times with the same result. Repeating it will not change the answer -- use what you have, change the arguments, or tell the user what is blocking you.]`
  const blockText = (n) => `[tool-guardian: this exact call was not run again -- it has returned the same result ${n} times. Use the result you already have, change the arguments, or tell the user what is blocking you.]`
  const hashesFor = key => recent.filter(entry => entry.key === key).map(entry => entry.hash)
  return {
    stats,
    warnText,
    blockText,
    identicalCount: span,   // how many identical results a refusal names: blockText(identicalCount)
    /** 'block' when this exact call already ran `span` times with one identical result. Stores nothing. */
    before(key) {
      const prior = hashesFor(key)
      const last = prior[prior.length - 1]
      if (prior.length < span || !prior.slice(-span).every(hash => hash === last)) return undefined
      stats.blocks += 1
      return 'block'
    },
    /** The warning to prepend, or '' -- and the call is remembered either way. */
    after(key, text) {
      // sha1 as a FINGERPRINT, not as a security primitive: it only has to tell
      // "this result is byte-identical to the last one" cheaply, and the result is
      // never hashed for anything that needs to be unforgeable.
      recent.push({ key, hash: createHash('sha1').update(String(text)).digest('hex') })
      while (recent.length > window) recent.shift()
      const same = hashesFor(key)
      const last = same[same.length - 1]
      let run = 1
      while (run < same.length && same[same.length - 1 - run] === last) run += 1
      if (run < warnAt) return ''
      stats.warnings += 1
      return warnText(run)
    },
  }
}

/** One number, counting anything missing or non-numeric as zero. */
const counter = value => (Number.isFinite(Number(value)) ? Number(value) : 0)

/**
 * The /toolguardian call-check line: what the router stopped, fixed, warned about
 * and refused, plus the same counters for the DSH-side guard (native), which the
 * Python router cannot see.
 */
export function callChecksLine(routerStats = {}, native = {}) {
  const r = routerStats ?? {}
  const n = native ?? {}
  return `call checks: ${counter(r.schema_rejects)} bad calls stopped before the server, `
    + `${counter(r.schema_coercions)} fixed silently, `
    + `${counter(r.loop_warnings) + counter(n.warnings)} repeat warnings, `
    + `${counter(r.loop_blocks) + counter(n.blocks)} repeats refused`
}

/**
 * Deny `names` for one agent. The registry refuses a restriction that names a tool this
 * agent cannot see, and says which names it does know -- so on that refusal the known list
 * is read from the message and the intersection is retried once. No registry internals and
 * no extra host import are needed to learn an agent's tool set.
 * @returns {{ dispose: (() => void) | null, denied: string[] }}
 */
export function restrictKnown(tools, names) {
  const wanted = [...new Set(names)]
  if (wanted.length === 0) return { dispose: null, denied: [] }
  try {
    return { dispose: tools.restrict({ deny: wanted }), denied: wanted }
  } catch (error) {
    const listed = /known global tools: (.*)$/s.exec(String(error?.message ?? ''))
    if (listed === null) throw error
    const known = new Set(listed[1].split(',').map(name => name.trim()).filter(Boolean))
    const present = wanted.filter(name => known.has(name))
    if (present.length === 0) return { dispose: null, denied: [] }
    return { dispose: tools.restrict({ deny: present }), denied: present }
  }
}

/** A .venv beside this package when one exists (a git checkout), else PATH. */
function defaultPython() {
  const venv = process.platform === 'win32'
    ? join(PACKAGE_ROOT, '.venv', 'Scripts', 'python.exe')
    : join(PACKAGE_ROOT, '.venv', 'bin', 'python')
  if (existsSync(venv)) return venv
  return process.platform === 'win32' ? 'python' : 'python3'
}

/**
 * The Python child and its request/response plumbing. One JSON object per
 * line each way; see modules/tg_bridge.py for the op list.
 */
export class PythonBridge {
  constructor(options, logger) {
    this.options = options
    this.logger = logger
    this.child = null
    this.nextId = 1
    this.pending = new Map()
    this.stopped = false
    this.routerReady = null   // promise of the `start` reply for the CURRENT child
  }

  /** Spawn on first use. Idempotent. */
  ensureStarted() {
    if (this.child !== null) return
    if (this.stopped) throw new Error('tool-guardian bridge is stopped')
    const { options } = this
    const env = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
    // Pass settings down under the names tool_guardian.py already reads, without
    // clobbering a value the operator exported for the .bat launchers.
    env.TOOL_GUARDIAN_START_TIMEOUT ??= String(options.startTimeoutMs / 1000)
    env.TOOL_GUARDIAN_CALL_TIMEOUT ??= String(options.callTimeoutMs / 1000)
    if (options.logPath) env.TOOL_GUARDIAN_LOG ??= options.logPath
    if (options.skillsDirs.length > 0) {
      env.TOOL_GUARDIAN_SKILLS ??= options.skillsDirs.join(process.platform === 'win32' ? ';' : ':')
    }
    if (!options.ladder.enabled) env.TOOL_GUARDIAN_LADDER = '0'
    if (options.spillDir) env.TOOL_GUARDIAN_SPILL_DIR ??= options.spillDir
    const child = spawn(options.python, [BRIDGE_SCRIPT], {
      cwd: PACKAGE_ROOT,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      // No console window, no focus steal; the MCP backends the bridge starts
      // inherit this hidden console rather than opening their own.
      windowsHide: true,
    })
    this.child = child
    createInterface({ input: child.stdout }).on('line', line => this.onLine(line))
    createInterface({ input: child.stderr }).on('line', line => this.logger.debug(`[bridge] ${line}`))
    child.on('error', error => this.onExit(`spawn failed (${options.python}): ${error.message}`))
    child.on('exit', (code, signal) => this.onExit(`exited code=${code} signal=${signal}`))
  }

  onLine(line) {
    let frame
    try {
      frame = JSON.parse(line)
    } catch {
      this.logger.warn(`tool-guardian bridge: non-JSON line on stdout dropped (${line.slice(0, 120)})`)
      return
    }
    const waiter = this.pending.get(frame.id)
    if (waiter === undefined) return
    this.pending.delete(frame.id)
    clearTimeout(waiter.timer)
    if (frame.ok === true) waiter.resolve(frame.result)
    else waiter.reject(new Error(String(frame.error ?? 'bridge error')))
  }

  onExit(reason) {
    if (this.child === null) return
    this.child = null
    this.routerReady = null   // a respawned child has no backends until `start` runs again
    // Loud, never silent: a dead router must not look like an empty tool list.
    for (const waiter of this.pending.values()) {
      clearTimeout(waiter.timer)
      waiter.reject(new Error(`tool-guardian bridge ${reason}`))
    }
    this.pending.clear()
    if (!this.stopped) this.logger.error(`tool-guardian bridge ${reason}`)
  }

  /**
   * Send one op and await its reply. Ops that need the router wait for `start`
   * first -- including after a crash: the child is respawned on demand and its
   * backends restarted, so a dead bridge heals on the next call instead of
   * answering "call before start" forever.
   */
  async request(op, params = {}, timeoutMs = this.options.callTimeoutMs + 5000) {
    if (!['hello', 'start', 'log', 'shutdown'].includes(op)) await this.startRouter()
    return this.send(op, params, timeoutMs)
  }

  send(op, params = {}, timeoutMs = this.options.callTimeoutMs + 5000) {
    this.ensureStarted()
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`tool-guardian bridge: "${op}" timed out after ${timeoutMs}ms`))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      this.child.stdin.write(`${JSON.stringify({ id, op, ...params })}\n`)
    })
  }

  /** Start the router's backends (inline mcpServers wins over a config path). */
  startRouter() {
    this.ensureStarted()
    if (this.routerReady !== null) return this.routerReady
    const { mcpServers, configPath, startTimeoutMs } = this.options
    const inline = Object.keys(mcpServers).length > 0
    const params = inline ? { mcpServers } : { config: configPath }
    params.options = {
      ladder: this.options.ladder.options,
      groups: this.options.groups,
      spillDir: this.options.spillDir,
      spillKeep: this.options.spillKeep,
    }
    // Backends start one after another inside the bridge; budget for all of them.
    const count = inline ? Object.keys(mcpServers).length : 8
    const started = this.send('start', params, startTimeoutMs * count + 5000)
    this.routerReady = started
    started.catch(() => {
      if (this.routerReady === started) this.routerReady = null   // let the next call retry
    })
    return started
  }

  /** Ask the child to stop its backends, then make sure it is gone. */
  stop() {
    this.stopped = true
    const child = this.child
    if (child === null) return
    try {
      child.stdin.write(`${JSON.stringify({ id: 0, op: 'shutdown' })}\n`)
      child.stdin.end() // stdin EOF also stops the backends (tg_bridge.serve finally)
    } catch { /* already gone */ }
    const killer = setTimeout(() => child.kill(), 3000)
    killer.unref()
    child.once('exit', () => clearTimeout(killer))
  }
}

/** Router tools return plain text; the canonical value IS the text. */
const TEXT_OUTPUT = {
  schema: { type: 'string' },
  render: (_args, value) => [{ type: 'text', text: String(value) }],
}

/** DeepSeek function names: [A-Za-z0-9_-], at most 64 chars. */
export function directToolName(server, tool) {
  return `tg__${server}__${tool}`.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64)
}

/** Text of a result when EVERY block is text; undefined otherwise (images etc. are never touched). */
export function flattenText(content) {
  if (!Array.isArray(content) || content.length === 0) return undefined
  if (!content.every(block => block?.type === 'text' && typeof block.text === 'string')) return undefined
  return content.map(block => block.text).join('\n')
}

/** Marks left by this plugin or the Python router: already shaped, never shape twice. */
const HANDLED_RE = /\[tool-guardian: |Full original result stored at:/

/**
 * The DSH spill policy's trailing notice. It fires at 50,000 bytes and leaves a ~46 KB
 * preview inline -- about 12,500 tokens, seen live on a `dir` -- which is most of a local
 * model's window. The full original is already in the harness spill file, so the preview is
 * shaped WITHOUT a second archive and the harness's own locator line is kept verbatim.
 */
const HARNESS_NOTICE_RE = /\s*\((?:Omitted \d+ bytes\. )?Full formatted result stored at: [^\n]*\)\s*$/

/**
 * Build the shell-bypass matcher: explicit rules first, then every catalogue
 * tool name (long enough not to be noise) as a whole word.
 * @returns (commandText) => { server, tool } | undefined
 */
export function buildBypassMatcher(bypass, groups) {
  const rules = []
  for (const rule of bypass.rules) {
    try {
      rules.push({ re: new RegExp(rule.pattern, 'i'), server: rule.server, tool: rule.tool })
    } catch { /* an invalid user regex disables that one rule, not the plugin */ }
  }
  const seen = new Set()
  for (const group of groups) {
    for (const full of group.tools) {
      const dot = full.indexOf('.')
      const server = full.slice(0, dot)
      const tool = full.slice(dot + 1)
      if (tool.length < bypass.minNameLength || seen.has(full)) continue
      seen.add(full)
      const escaped = tool.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      rules.push({ re: new RegExp(`(^|[^A-Za-z0-9_])${escaped}([^A-Za-z0-9_]|$)`), server, tool })
    }
  }
  return (text) => {
    if (typeof text !== 'string' || text.length === 0) return undefined
    const hit = rules.find(rule => rule.re.test(text))
    return hit === undefined ? undefined : { server: hit.server, tool: hit.tool }
  }
}

export function apply(ctx, config) {
  // The authoritative section: the patch-row config until a settings provider
  // attaches, the resolved settings scope while one is present.
  let source = () => config
  let bridge = null
  let generation = 0            // bumped on every (re)start so a stale start cannot register
  let ready = false             // backends reported; ladder + router calls may use the bridge
  let matchBypass = () => undefined
  const ownTools = new Set()    // names this plugin registered (their results are shaped in Python)
  const disposers = new Map()   // tool name -> unregister
  const activeGroups = new Set()
  const nudges = new Map()      // callId -> { server, tool } awaiting its post-execute
  const masks = new Map()       // agent -> { hidden: Set<group>, dispose, denied: string[] }
  let session = ''              // the Python router's session id (every call-log row carries it)
  let restoreOffer = null       // { session, updated, groups } from the previous session, or null
  let bootError = ''            // why the last start failed, for /toolguardian
  // One guard per plugin instance, rebuilt with the options whenever the bridge restarts.
  let loopGuard = createLoopGuard(resolveOptions(source()).loopGuard)

  /** The guard is on unless the settings or the environment say otherwise. */
  const loopActive = () => {
    // $TG_LOOP_GUARD is read HERE, not at boot: it can be switched off mid-session,
    // and the settings it was resolved from stay authoritative either way.
    if (LOOP_OFF.has(String(process.env.TG_LOOP_GUARD ?? '').trim().toLowerCase())) return false
    return (bridge?.options?.loopGuard ?? resolveOptions(source()).loopGuard).enabled !== false
  }

  /** One call as a comparable string: who asked, which tool, with which arguments. */
  const loopKey = (exec) => `${exec.agent?.session?.id ?? exec.sessionId ?? ''}\u0000${exec.name}\u0000${stableJson(exec.arguments ?? {})}`

  /** before() that can only ever say "block" or nothing -- never throws, never changes a call. */
  const guardBefore = (exec) => {
    try {
      if (ownTools.has(exec.name) || !loopActive()) return undefined
      return loopGuard.before(loopKey(exec))
    } catch (error) {
      ctx.logger.debug(`tool-guardian loop guard: ${error.message}; the call runs`)
      return undefined
    }
  }

  /** after() that can only ever return a prefix or '' -- never throws, never changes a result. */
  const guardAfter = (exec, text) => {
    try {
      if (ownTools.has(exec.name) || !loopActive()) return ''
      return loopGuard.after(loopKey(exec), text) ?? ''
    } catch (error) {
      ctx.logger.debug(`tool-guardian loop guard: ${error.message}; the result is unchanged`)
      return ''
    }
  }

  /** (Re)apply one agent's mask: every tool of every still-hidden group is denied for it. */
  const applyMask = (agent, hidden) => {
    const previous = masks.get(agent)
    previous?.dispose?.()
    const { groups } = resolveOptions(source()).builtinGroups
    const names = [...hidden].flatMap(group => groups[group] ?? []).filter(name => !ownTools.has(name))
    const { dispose, denied } = restrictKnown(agent.ctx.tools, names)
    masks.set(agent, { hidden, dispose, denied })
    return denied
  }

  const clearMasks = () => {
    for (const mask of masks.values()) mask.dispose?.()
    masks.clear()
  }

  /** The built-in half of list_groups_with_costs, for the calling agent. */
  const builtinSection = (agent) => {
    const options = resolveOptions(source()).builtinGroups
    if (!options.enabled || Object.keys(options.groups).length === 0) return ''
    const hidden = masks.get(agent)?.hidden ?? new Set()
    const rows = Object.entries(options.groups).sort(([a], [b]) => a.localeCompare(b)).map(([group, names]) =>
      `${group}  ${names.length} tools  ${hidden.has(group) ? `HIDDEN -- activate_group(group="${group}") loads: ${names.join(', ')}` : 'active'}`)
    return `\nDSH built-in tool groups (hidden ones cost nothing until activated):\n${rows.join('\n')}\n`
  }

  const unregisterAll = () => {
    for (const dispose of disposers.values()) dispose()
    disposers.clear()
    ownTools.clear()
    activeGroups.clear()
  }

  const stopBridge = () => {
    generation += 1
    ready = false
    unregisterAll()
    if (bridge === null) return
    bridge.stop()
    bridge = null
  }

  const getBridge = () => {
    bridge ??= new PythonBridge(resolveOptions(source()), ctx.logger)
    return bridge
  }

  const register = (definition) => {
    if (disposers.has(definition.name)) return
    disposers.set(definition.name, ctx.tools.register(definition))
    ownTools.add(definition.name)
  }

  /** One router tool, schema and description straight from the Python router. */
  const routerTool = spec => ({
    name: spec.name,
    description: spec.description,
    parameters: spec.inputSchema ?? { type: 'object', properties: {} },
    output: TEXT_OUTPUT,
    // As tool OUTPUT, never a thrown error: the model must see a router failure and say so.
    execute: async (args, exec) => {
      const { text } = await getBridge().request('call', { name: spec.name, args: args ?? {} })
      if (spec.name !== 'list_groups_with_costs') return text
      const extra = builtinSection(exec?.agent) + restoreLine()
      if (extra === '') return text
      const at = text.lastIndexOf('NEXT STEP:')
      return at < 0 ? `${text}\n${extra}` : `${text.slice(0, at)}${extra.trimStart()}\n${text.slice(at)}`
    },
  })

  /** Register one group's tools as direct native tools; returns what was added. */
  const activateGroup = async (group) => {
    const { tools } = await getBridge().request('group_tools', { group })
    let cost = 0
    for (const tool of tools) {
      const name = directToolName(tool.server, tool.tool)
      cost += Math.ceil(JSON.stringify(tool).length / 3.5)
      register({
        name,
        description: `[${tool.server}] ${tool.description}`.slice(0, 1024),
        parameters: tool.inputSchema,
        output: TEXT_OUTPUT,
        execute: async args => (await getBridge().request('call', {
          name: 'call_tool', args: { server: tool.server, tool: tool.tool, args: args ?? {} },
        })).text,
      })
    }
    activeGroups.add(group)
    return { names: tools.map(tool => directToolName(tool.server, tool.tool)), cost }
  }

  const activateGroupTool = () => {
    const builtin = resolveOptions(source()).builtinGroups
    const hiddenNow = builtin.enabled ? builtin.hidden.filter(group => Object.hasOwn(builtin.groups, group)) : []
    return activateGroupDefinition(hiddenNow.length === 0 ? '' : ` Hidden built-in groups: ${hiddenNow.map(group => `${group} (${builtin.groups[group].join(', ')})`).join('; ')}.`)
  }

  const activateGroupDefinition = hiddenNote => ({
    name: 'activate_group',
    description: 'Load ONE tool group as direct tools (its schemas then ride every request -- '
      + `call list_groups_with_costs first and load only the group the task needs).${hiddenNote}`,
    parameters: {
      type: 'object',
      properties: { group: { type: 'string', description: 'group name from list_groups_with_costs' } },
      required: ['group'],
    },
    output: TEXT_OUTPUT,
    execute: async (args, exec) => {
      const group = String(args?.group ?? '')
      const builtin = resolveOptions(source()).builtinGroups
      if (builtin.enabled && Object.hasOwn(builtin.groups, group)) {
        const mask = exec?.agent === undefined ? undefined : masks.get(exec.agent)
        if (mask === undefined || !mask.hidden.has(group)) {
          return `built-in group "${group}" is already active.\n\nNEXT STEP: call its tools directly: ${builtin.groups[group].join(', ')}.`
        }
        const hidden = new Set(mask.hidden)
        hidden.delete(group)
        try {
          applyMask(exec.agent, hidden)
        } catch (error) {
          return `activate_group failed: ${error.message}\n\nNEXT STEP: list_groups_with_costs()`
        }
        return `built-in group "${group}" is active for this session: ${builtin.groups[group].join(', ')}.\n`
          + 'Their schemas ride every request from now on.\n\nNEXT STEP: call the tool you needed directly.'
      }
      if (activeGroups.has(group)) return `group "${group}" is already active.\n\nNEXT STEP: call its tools directly (names start with tg__).`
      try {
        const added = await activateGroup(group)
        if (added.names.length === 0) return `group "${group}" has no reachable tools.\n\nNEXT STEP: list_groups_with_costs()`
        recordActivation(group)
        return `group "${group}" is active: ${added.names.length} tools, ~${added.cost} tokens added to every request from now on.\n`
          + `${added.names.join(', ')}\n\nNEXT STEP: call ${added.names[0]} (or another of the tools above) directly.`
      } catch (error) {
        return `activate_group failed: ${error.message}\n\nNEXT STEP: list_groups_with_costs()`
      }
    },
  })

  /** Remember a runtime activation for the next session's restore offer. Never blocks, never throws. */
  const recordActivation = (group) => {
    bridge?.request('activated', { group }, 5000).catch(() => {})
  }

  /** The restore offer as one line for list_groups_with_costs; '' when there is nothing to offer. */
  const restoreLine = () => {
    const pending = (restoreOffer?.groups ?? []).filter(group => !activeGroups.has(group))
    if (pending.length === 0 || !disposers.has('restore_groups')) return ''
    return `\nLast session used: ${pending.join(', ')} -- restore_groups() loads them again (optional).\n`
  }

  /**
   * Re-activate the previous session's groups -- only when the model or the user asks.
   * @returns the tool/command text
   */
  const restoreGroups = async () => {
    const offer = restoreOffer ?? (await getBridge().request('restore_offer', {}, 10000)).offer
    const groups = offer?.groups ?? []
    if (groups.length === 0) {
      return 'nothing to restore: no earlier session activated a group.\n\nNEXT STEP: list_groups_with_costs()'
    }
    const loaded = []
    const skipped = []
    let cost = 0
    for (const group of groups) {
      if (activeGroups.has(group)) {
        skipped.push(`${group} (already active)`)
        continue
      }
      try {
        const added = await activateGroup(group)
        if (added.names.length === 0) {
          skipped.push(`${group} (no reachable tools now)`)
          continue
        }
        recordActivation(group)
        loaded.push(`${group} (${added.names.length} tools)`)
        cost += added.cost
      } catch (error) {
        skipped.push(`${group} (${error.message})`)
      }
    }
    const lines = [`restore_groups: the session of ${offer.updated ?? 'earlier'} used ${groups.join(', ')}.`]
    if (loaded.length > 0) lines.push(`loaded: ${loaded.join(', ')} -- ~${cost} tokens added to every request from now on.`)
    if (skipped.length > 0) lines.push(`skipped: ${skipped.join(', ')}.`)
    lines.push('', loaded.length > 0
      ? 'NEXT STEP: call the tools you need directly (names start with tg__).'
      : 'NEXT STEP: list_groups_with_costs()')
    return lines.join('\n')
  }

  const restoreGroupsDefinition = () => ({
    name: 'restore_groups',
    description: 'Load the tool groups the PREVIOUS session activated (list_groups_with_costs shows them and their cost). '
      + 'Optional: call it only when this task needs the same tools.',
    parameters: { type: 'object', properties: {} },
    output: TEXT_OUTPUT,
    execute: async () => {
      try {
        return await restoreGroups()
      } catch (error) {
        return `restore_groups failed: ${error.message}\n\nNEXT STEP: list_groups_with_costs()`
      }
    },
  })

  /** Spawn, start the backends, then (and only then) advertise tools that can run. */
  const startRouter = async () => {
    const mine = ++generation
    const live = getBridge()
    const options = live.options
    const hello = await live.request('hello', {}, 15000)
    ctx.logger.info(`tool-guardian bridge up: tool_guardian ${hello.tool_guardian}, python ${hello.python} (${hello.executable})`)
    const status = await live.startRouter()
    if (mine !== generation) return
    session = typeof status.session === 'string' ? status.session : ''
    const rows = Object.entries(status.backends).map(([id, b]) => `${id}=${b.status}(${b.tools})`)
    ctx.logger.info(`tool-guardian backends: ${rows.join(' ') || 'none configured'}`)
    const { tools } = await live.request('tools')
    if (mine !== generation) return
    for (const spec of tools) register(routerTool(spec))
    if (options.allowRuntimeActivation) {
      register(activateGroupTool())
      register(restoreGroupsDefinition())
    }
    let groups = []
    try {
      groups = (await live.request('groups')).groups
    } catch (error) {
      ctx.logger.warn(`tool-guardian: group manifest unavailable (${error.message})`)
    }
    if (mine !== generation) return
    matchBypass = buildBypassMatcher(options.bypass, groups)
    for (const group of options.activeGroups) {
      try {
        const added = await activateGroup(group)
        ctx.logger.info(`tool-guardian: group "${group}" active at load (${added.names.length} tools, ~${added.cost} tokens)`)
      } catch (error) {
        ctx.logger.warn(`tool-guardian: activeGroups entry "${group}" not loaded (${error.message})`)
      }
    }
    ready = true
    bootError = ''
    ctx.logger.info(`tool-guardian ready: ${ownTools.size} native tools registered; ladder ${options.ladder.enabled ? 'on' : 'off'}; bypass mode ${options.bypass.mode}`)
    // Offered, never forced: the previous session's groups are named, not loaded.
    try {
      restoreOffer = (await live.request('restore_offer', {}, 10000)).offer ?? null
    } catch {
      restoreOffer = null
    }
    if (mine !== generation) return
    const pending = (restoreOffer?.groups ?? []).filter(group => !activeGroups.has(group))
    if (pending.length > 0 && options.allowRuntimeActivation) {
      ctx.logger.info(`tool-guardian: last session used groups ${pending.join(', ')} -- restore_groups() or /toolguardian restore loads them (not loaded automatically)`)
    }
  }

  const boot = () => {
    startRouter().catch((error) => {
      // Loud, and nothing registered: a dead router must not look like an empty tool list.
      bootError = error.message
      ctx.logger.error(`tool-guardian failed to start: ${error.message}`)
    })
  }

  // installSection fires onChange at attach, at detach and on every commit --
  // including ones that leave the resolved options untouched. Recycle only when
  // a fact actually moved, or a settings provider attaching a moment after boot
  // would kill the start mid-handshake.
  const recycleIfChanged = () => {
    if (bridge === null) return
    if (JSON.stringify(resolveOptions(source())) === JSON.stringify(bridge.options)) return
    // The guard's thresholds came from the options that just went away: new guard, new counts.
    loopGuard = createLoopGuard(resolveOptions(source()).loopGuard)
    stopBridge()
    boot()
  }

  // ── output ladder for EVERY tool (the dsh-trim role) ───────────────────────
  ctx.on('tools/post-execute', async (exec, result, next) => {
    const decision = await next()            // exactly once; everything below fails open
    try {
      const nudge = nudges.get(exec.callId)
      nudges.delete(exec.callId)
      const options = bridge?.options
      if (!ready || options === undefined) return decision
      if (decision.kind !== 'accept' || Object.hasOwn(decision, 'value')) return decision
      let text = flattenText(decision.content ?? result.content)
      if (text === undefined) return decision
      let changed = false
      // Fingerprinted on the result as the tool returned it, and prepended only at
      // the end: the ladder below must see the original bytes, not a warning about them.
      const loopPrefix = guardAfter(exec, text)
      const skip = ownTools.has(exec.name) || options.ladder.skipTools.includes(exec.name)
      const floor = result.isError ? 300 : (options.ladder.options.compact_above_chars ?? 1200)
      if (options.ladder.enabled && options.ladder.allTools && !skip && text.length > floor && !HANDLED_RE.test(text)) {
        const notice = HARNESS_NOTICE_RE.exec(text)
        const body = notice === null ? text : text.slice(0, notice.index)
        const shaped = await bridge.request('ladder', { text: body, tool: exec.name, is_error: result.isError === true, archive: notice === null }, 20000)
        if (typeof shaped.text === 'string' && shaped.text !== body) {
          text = notice === null ? shaped.text : `${shaped.text}\n${notice[0].trim()}`
          changed = true
        }
      }
      if (nudge !== undefined) {
        text += `\n\nNEXT STEP: "${nudge.tool}" is a router tool -- next time call call_tool(server="${nudge.server}", tool="${nudge.tool}", args={...}) instead of the shell.`
        changed = true
      }
      if (loopPrefix !== '') {
        text = `${loopPrefix}\n${text}`
        changed = true
      }
      if (!changed) return decision
      return {
        kind: 'accept',
        content: [{ type: 'text', text }],
        ...(decision.additionalContexts ? { additionalContexts: decision.additionalContexts } : {}),
      }
    } catch (error) {
      ctx.logger.warn(`tool-guardian ladder: ${exec.name}: ${error.message}; keeping the original result`)
      return decision
    }
  }, { prepend: true })

  // ── router bypass: a shell call doing a router tool's job ──────────────────
  ctx.on('tools/pre-execute', async (exec, next) => {
    const decision = await next()
    // Before the bypass rules, and before they can return early: a call that would only
    // repeat the last identical result is refused whether or not bypass watching is on.
    // The router's own tools are left alone -- Python already guards those.
    if (decision.kind === 'allow' && guardBefore(exec) === 'block') {
      return { kind: 'deny', reason: loopGuard.blockText(loopGuard.identicalCount) }
    }
    try {
      const options = bridge?.options
      if (!ready || options === undefined || options.bypass.mode === 'off') return decision
      if (decision.kind !== 'allow' || !options.bypass.shellTools.includes(exec.name)) return decision
      const a = exec.arguments ?? {}
      const hit = matchBypass(a.command ?? a.script ?? a.code)
      if (hit === undefined) return decision
      const mode = options.bypass.mode
      bridge.request('log', { entry: { kind: 'bypass', tool: exec.name, server: hit.server, target: hit.tool, mode, ok: mode !== 'deny' } }, 5000).catch(() => {})
      if (mode === 'deny') {
        return { kind: 'deny', reason: `"${hit.tool}" is a router tool. NEXT STEP: call_tool(server="${hit.server}", tool="${hit.tool}", args={...}) -- describe_tool(server="${hit.server}", tool="${hit.tool}") shows the arguments.` }
      }
      if (mode === 'nudge') nudges.set(exec.callId, hit)
      return decision
    } catch (error) {
      ctx.logger.warn(`tool-guardian bypass watch: ${error.message}`)
      return decision
    }
  })

  // ── built-in tool groups: DSH's own tools stay out of a request until asked for ──
  // A restriction is per AGENT (the registry refuses a context-global one), so it is
  // applied as each agent is created and lifted per agent by activate_group.
  ctx.on('agent/created', ({ agent }) => {
    try {
      const options = resolveOptions(source()).builtinGroups
      if (!options.enabled || options.hidden.length === 0) return
      const denied = applyMask(agent, new Set(options.hidden.filter(group => Object.hasOwn(options.groups, group))))
      ctx.logger.debug(`tool-guardian: ${denied.length} built-in tools hidden for a new agent (${denied.join(', ')})`)
    } catch (error) {
      ctx.logger.warn(`tool-guardian built-in groups: ${error.message}; this agent keeps every tool`)
    }
  })
  ctx.on('agent/disposed', ({ agent }) => {
    masks.get(agent)?.dispose?.()
    masks.delete(agent)
  })

  // Optional settings: the plugin runs from its patch-row config when no
  // provider is loaded, and re-reads when one attaches, changes or detaches.
  ctx.inject(['settings'], (settingsCtx) => {
    settingsCtx.settings.installSection(ctx, SETTINGS_NAMESPACE, Config, config, {
      setSource: (current) => {
        source = current
      },
      onChange: recycleIfChanged,
    })
  })

  // ── /toolguardian: the selftest, bypass summary and restore, for the human ──
  const USAGE = 'usage: /toolguardian [selftest] | /toolguardian bypass [last | 24h] | /toolguardian restore'

  /** The DSH-side selftest: what `tool-guardian --selftest` proves, from the live bridge. */
  const selftestReport = async () => {
    const live = bridge
    if (live === null || !ready) {
      const why = bootError === '' ? 'still starting -- try again in a few seconds' : `failed to start: ${bootError}`
      return { kind: 'error', text: `tool-guardian ${PKG_VERSION}: ${why}.\nThe DSH log has the detail (search "tool-guardian").` }
    }
    const options = live.options
    const lines = []
    const hello = await live.request('hello', {}, 15000)
    lines.push(`tool-guardian ${PKG_VERSION} (router ${hello.tool_guardian}, python ${hello.python})`)
    const status = await live.startRouter()
    const backends = Object.entries(status.backends ?? {})
    const up = backends.filter(([, b]) => b.status === 'ok')
    lines.push(`backends: ${up.length} up, ${backends.length - up.length} down`)
    for (const [id, b] of backends) {
      lines.push(`  ${id.padEnd(18)} ${String(b.status).padEnd(12)} ${b.status === 'ok' ? `${b.tools} tools` : String(b.error ?? '').slice(0, 160)}`)
    }
    if (backends.length === 0) lines.push('  none configured -- run `npm run setup` in the plugin folder, or see the README "Configure" section')
    try {
      const manifest = await live.request('groups', {}, 15000)
      const full = (manifest.groups ?? []).reduce((sum, group) => sum + (group.token_cost ?? 0), 0)
      const tools = (manifest.groups ?? []).reduce((sum, group) => sum + (group.tool_count ?? 0), 0)
      const router = manifest.router_cost ?? 0
      lines.push(`router tools: ${ownTools.size} native tools; the router's own schemas cost ~${router} tokens on every request`)
      if (full > router) {
        lines.push(`behind the router: ${tools} tools, ~${full} tokens of schemas -> ~${full - router} tokens freed on every request (${Math.round(100 * (1 - router / full))}% smaller)`)
      } else {
        lines.push(`behind the router: ${tools} tools, ~${full} tokens -- at this size the router saves little; it pays off as servers are added`)
      }
    } catch (error) {
      lines.push(`token saving: unavailable (${error.message})`)
    }
    lines.push(`active groups: ${activeGroups.size === 0 ? 'none (activate_group loads one)' : [...activeGroups].join(', ')}`)
    const pending = (restoreOffer?.groups ?? []).filter(group => !activeGroups.has(group))
    if (pending.length > 0) lines.push(`last session used: ${pending.join(', ')} -- /toolguardian restore (or the model's restore_groups) loads them`)
    try {
      const stats = await live.request('stats', {}, 10000)
      lines.push(`output ladder: ${options.ladder.enabled ? 'on' : 'off'} -- ${stats.shaped ?? 0} results shaped, ${stats.spilled ?? 0} archived, ${stats.original_chars ?? 0} -> ${stats.final_chars ?? 0} chars since load`)
      lines.push(callChecksLine(stats, loopGuard.stats))
    } catch { /* counters are a courtesy */ }
    try {
      const summary = await live.request('summary', { scope: 'current' }, 15000)
      lines.push(`bypass watch: ${options.bypass.mode} -- ${summary.summary?.bypasses ?? 0} shell calls did a router tool's job this session (/toolguardian bypass)`)
    } catch { /* the summary has its own command */ }
    try {
      lines.push((await live.request('update', {}, 8000)).line)
    } catch {
      lines.push('update: could not check')
    }
    lines.push('', 'This proves the saving and that the backends start. It does NOT prove your model will call the router:',
      'run a few real tasks, then /toolguardian bypass shows whether it did.')
    return { kind: 'success', text: lines.join('\n') }
  }

  const bypassReport = async (arg) => {
    if (bridge === null || !ready) return { kind: 'error', text: 'tool-guardian is not ready yet -- /toolguardian shows why.' }
    const scope = arg === 'last' ? 'previous' : /^\d+h$/.test(arg) ? `since:${Number.parseInt(arg, 10)}` : 'current'
    const result = await bridge.request('summary', { scope }, 15000)
    return { kind: 'success', text: result.text }
  }

  const toolguardianCommand = async (invocation) => {
    const [sub = '', arg = ''] = String(invocation?.rawInput ?? '').trim().toLowerCase().split(/\s+/)
    try {
      if (sub === '' || sub === 'selftest' || sub === 'status') return await selftestReport()
      if (sub === 'bypass') return await bypassReport(arg)
      if (sub === 'restore') {
        if (bridge === null || !ready) return { kind: 'error', text: 'tool-guardian is not ready yet -- /toolguardian shows why.' }
        if (!resolveOptions(source()).allowRuntimeActivation) return { kind: 'error', text: 'restore needs allowRuntimeActivation: true.' }
        return { kind: 'success', text: await restoreGroups() }
      }
      return { kind: 'error', text: `unknown subcommand "${sub}". ${USAGE}` }
    } catch (error) {
      return { kind: 'error', text: `/toolguardian ${sub}: ${error.message}` }
    }
  }

  // Optional like settings: a host without the command registry still gets the tools.
  ctx.inject(['commands'], (inner) => {
    inner.commands.register({
      name: 'toolguardian',
      description: 'Tool Guardian: selftest (backends, token saving, update) · bypass [last|24h] · restore',
      // Without `input` DSH treats a command as argument-free and sends "/toolguardian bypass" to the MODEL.
      input: { hint: '[selftest | bypass | bypass last | restore]' },
      handler: toolguardianCommand,
    })
  })

  // Registrations are effects: unloading or hot-replacing the plugin unregisters
  // the tools and stops the Python child, which stops the MCP backends behind it.
  ctx.effect(() => () => {
    clearMasks()
    stopBridge()
  })

  // The router tools cannot exist before the backends answer, so start now
  // unless the operator asked for a lazy bridge (then nothing is advertised
  // until something else wakes it -- mostly useful for tests).
  // activate_group must exist from the first request when built-in groups are hidden:
  // a session opened while the backends are still starting would otherwise have tools
  // hidden and no way to get them back.
  const initial = resolveOptions(source())
  if (initial.allowRuntimeActivation && initial.builtinGroups.enabled) register(activateGroupTool())
  if (initial.eagerStart) boot()

  // Fire-and-forget: the update notice is a courtesy line, never a reason to wait.
  void checkForUpdate({
    pkg: name,
    current: PKG_VERSION,
    cacheFile: defaultCacheFile(name),
    log: (line) => ctx.logger.info(line),
  }).catch(() => {})
}
