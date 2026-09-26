// Contract probe for setup.mjs (`npm run setup`): finds Python like the plugin does, runs
// tg_setup.py import then doctor beside itself, returns the doctor's exit code. No real process runs.
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

let pass = 0, total = 0
const check = (name, cond) => { total++; if (cond) pass++; console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${name}`) }
let setup = {}
try { setup = await import('../setup.mjs') } catch (error) { console.log(`  (setup.mjs not importable: ${error.message})`) }

const fakeRun = (versions, doctorStatus = 0) => {
  const calls = []
  const run = (command, args, options = {}) => {
    calls.push({ command, args, options })
    if (args[0] === '--version') {
      const v = versions[command]
      return v === undefined ? { status: null, error: new Error('ENOENT') } : { status: 0, stdout: `Python ${v}\n`, stderr: '' }
    }
    if (args[1] === 'doctor') return { status: doctorStatus }
    return { status: 0 }
  }
  return { run, calls }
}
const root = mkdtempSync(join(tmpdir(), 'tg-setup-probe-'))
const logs = []
const log = line => logs.push(line)

check('exports_main_and_find_python', typeof setup.main === 'function' && typeof setup.findPython === 'function' && typeof setup.pythonCandidates === 'function')
const env = { TOOL_GUARDIAN_PYTHON: 'C:/py311/python.exe' }
check('env_python_is_tried_first', setup.pythonCandidates?.(env, root, 'win32')?.[0] === 'C:/py311/python.exe'
  && setup.pythonCandidates?.({}, root, 'linux')?.[0] === 'python3')
mkdirSync(join(root, '.venv', 'bin'), { recursive: true }); writeFileSync(join(root, '.venv', 'bin', 'python'), '')
check('venv_beside_the_package_before_path', setup.pythonCandidates?.({}, root, 'linux')?.[0] === join(root, '.venv', 'bin', 'python'))
check('python_older_than_3_9_is_skipped', setup.findPython?.(['old', 'new'], fakeRun({ old: '3.8.10', new: '3.11.9' }).run) === 'new'
  && setup.findPython?.(['none'], fakeRun({}).run) === null)

const ok = fakeRun({ python3: '3.11.9' }, 0)
const code = setup.main?.(['--yes'], { log, run: ok.run, env: {}, root: join(root, 'pkg'), platform: 'linux' })
const work = ok.calls.filter(c => c.args[0] !== '--version')
check('import_then_doctor_with_args_passed_through', code === 0 && work.length === 2
  && work[0].args[0] === join(root, 'pkg', 'tg_setup.py') && work[0].args.slice(1).join(' ') === 'import --yes'
  && work[1].args.slice(1).join(' ') === 'doctor' && work.every(c => c.options.stdio === 'inherit' && c.options.cwd === join(root, 'pkg')))
check('prints_the_dsh_next_step', logs.some(l => l.includes('/toolguardian')) && logs.some(l => l.includes('~/.tool-guardian/mcp.json')))
const bad = fakeRun({ python3: '3.11.9' }, 1)
check('exit_code_is_the_doctors', setup.main?.([], { log, run: bad.run, env: {}, root, platform: 'linux' }) === 1)
const only = fakeRun({ python3: '3.11.9' }, 0)
setup.main?.(['--doctor'], { log, run: only.run, env: {}, root, platform: 'linux' })
check('doctor_only_skips_import', only.calls.filter(c => c.args[0] !== '--version').map(c => c.args[1]).join() === 'doctor')
logs.length = 0
const none = fakeRun({}, 0)
check('no_python_is_exit_1_with_a_fix', setup.main?.([], { log, run: none.run, env: {}, root, platform: 'linux' }) === 1
  && logs.some(l => l.startsWith('FAIL no Python')) && logs.some(l => l.includes('fix:')))

console.log(`probe_setup: ${total} checks, ${pass} passed, ${total - pass} failed`)
process.exit(pass === total ? 0 : 1)
