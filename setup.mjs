#!/usr/bin/env node
/**
 * `npm run setup` for dsh-tool-guardian: the one command after `dsh plugin add`.
 *
 *   npm run setup                 copy the MCP servers you already use (Claude Desktop, Cursor,
 *                                 Windsurf, ./.mcp.json) into ~/.tool-guardian/mcp.json -- it asks
 *                                 before writing -- then check them with the doctor
 *   npm run setup -- --yes        the same, without the question
 *   npm run setup -- --from PATH  import from one specific client config
 *   npm run setup -- --doctor     only the check
 *
 * It finds Python the same way the plugin does ($TOOL_GUARDIAN_PYTHON, a .venv beside this
 * package, then python / python3 on PATH) and runs tg_setup.py beside it, so nobody has to
 * know where DSH put the package. No DSH preset is edited: the plugin's cordis.patch.yml
 * already adds its row to the profile, and the bridge finds ~/.tool-guardian/mcp.json by itself.
 *
 * Node 22+, standard library only. MIT licensed.
 */
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const PACKAGE_ROOT = dirname(fileURLToPath(import.meta.url))

/** Interpreters to try, in order. The first that reports Python >= 3.9 wins. */
export function pythonCandidates(env = process.env, root = PACKAGE_ROOT, platform = process.platform) {
  const out = []
  if (env.TOOL_GUARDIAN_PYTHON?.trim()) out.push(env.TOOL_GUARDIAN_PYTHON.trim())
  const venv = platform === 'win32' ? join(root, '.venv', 'Scripts', 'python.exe') : join(root, '.venv', 'bin', 'python')
  if (existsSync(venv)) out.push(venv)
  out.push(...(platform === 'win32' ? ['python', 'python3'] : ['python3', 'python']))
  return out
}

const defaultRun = (command, args, options = {}) => spawnSync(command, args, { windowsHide: true, encoding: 'utf8', ...options })

/** @returns the first candidate whose `--version` is Python 3.9+, or null. */
export function findPython(candidates, run = defaultRun) {
  for (const candidate of candidates) {
    const result = run(candidate, ['--version'])
    const text = `${result.stdout ?? ''}${result.stderr ?? ''}`
    const match = /Python (\d+)\.(\d+)/.exec(text)
    if (result.status === 0 && match !== null && (Number(match[1]) > 3 || (Number(match[1]) === 3 && Number(match[2]) >= 9))) {
      return candidate
    }
  }
  return null
}

export const NEXT_STEPS = [
  '',
  'Next:',
  '  1. Start a NEW DSH session in the profile you added dsh-tool-guardian to (for example `dsh web`).',
  '  2. Type /toolguardian -- it lists your servers and the tokens the router saves on every request.',
  'DSH finds ~/.tool-guardian/mcp.json by itself; the client snippet printed above is only for',
  'other MCP clients (Claude Code, Cursor) if you also run tool-guardian there.',
  'Add one more server later: python tg_setup.py add <name> -- <command> [args...] (in this folder).',
]

/**
 * @param argv - arguments after `npm run setup --`
 * @param io - injectable log / run / env for tests
 * @returns exit code: the doctor's (0 = no errors), or 1 when no usable Python was found
 */
export function main(argv, io = {}) {
  const log = io.log ?? console.log
  const run = io.run ?? defaultRun
  const env = io.env ?? process.env
  const root = io.root ?? PACKAGE_ROOT
  const python = findPython(pythonCandidates(env, root, io.platform ?? process.platform), run)
  if (python === null) {
    log('FAIL no Python 3.9+ found (tried $TOOL_GUARDIAN_PYTHON, a .venv beside this package, python, python3).')
    log('     fix: install Python 3.9 or later and make sure `python --version` works, or set TOOL_GUARDIAN_PYTHON')
    return 1
  }
  const script = join(root, 'tg_setup.py')
  const args = [...argv]
  const doctorOnly = args.includes('--doctor')
  const importArgs = args.filter(arg => arg !== '--doctor')
  const inherit = { stdio: 'inherit', cwd: root, env: { ...env, PYTHONIOENCODING: 'utf-8' } }
  log(`tool-guardian setup (python: ${python})`)
  if (!doctorOnly) {
    log('\n== import: the MCP servers you already use ==')
    run(python, [script, 'import', ...importArgs], inherit)   // "nothing found" is not fatal: doctor says what to do
  }
  log('\n== doctor ==')
  const doctor = run(python, [script, 'doctor'], inherit)
  for (const line of NEXT_STEPS) log(line)
  return typeof doctor.status === 'number' ? doctor.status : 1
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(main(process.argv.slice(2)))
}
