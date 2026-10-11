// tests/probe_recall_line.mjs -- contract probe for the /toolguardian report lines (written from the contract).
import * as plugin from 'dsh-tool-guardian'
let pass = 0, total = 0
const check = (name, cond) => { total++; if (cond) pass++; console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${name}`) }
const f = plugin.recallLine
check('exported', typeof f === 'function')
if (typeof f === 'function') {
  check('empty_is_empty_string', f({}) === '' && f(undefined) === '')
  check('exact_line', f({ bash: { spilled: 14, recalled: 2 }, web_fetch: { spilled: 3, recalled: 0 } })
    === 'recall after shortening: bash 2 of 14 (14%), web_fetch 0 of 3 (0%)')
  const many = Object.fromEntries(['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((k, i) => [k, { spilled: 10 - i, recalled: 1 }]))
  const line = f(many)
  check('top_five_by_spilled', line.startsWith('recall after shortening: a 1 of 10 (10%), b 1 of 9 (11%)') && !line.includes(' f ') && line.endsWith('(+2 more)'))
  check('ties_sorted_by_name', f({ zed: { spilled: 2, recalled: 0 }, abe: { spilled: 2, recalled: 1 } }) === 'recall after shortening: abe 1 of 2 (50%), zed 0 of 2 (0%)')
  check('bad_values_count_as_zero', f({ x: { spilled: 'nope' } }) === '' )
}
const L = plugin.ladderLine
check('ladder_line_exported', typeof L === 'function')
if (typeof L === 'function') {
  check('ladder_line_counts_archive_failures',
    L(true, { shaped: 3, spilled: 2, archive_failures: 1, original_chars: 900, final_chars: 300 })
      === 'output ladder: on -- 3 results shaped, 2 archived, 1 archive failures, 900 -> 300 chars since load')
  check('ladder_line_missing_counts_are_zero',
    L(false, {}) === 'output ladder: off -- 0 results shaped, 0 archived, 0 archive failures, 0 -> 0 chars since load')
}
console.log(`probe_recall_line: ${total} checks, ${pass} passed, ${total - pass} failed`)
process.exit(pass === total && total > 0 ? 0 : 1)
