import { pathToFileURL } from 'node:url'
import { parseTextToolCalls } from './text_toolcalls.js'

// A text tool call is written by the model either as `<function=NAME>...` or
// wrapped in a tool_call element whose tag carries a zero-width space
// (`<​tool_call>` / `</​tool_call>`), so accept both spellings.
const ZWSP = ''

export const TRIGGER = new RegExp('<function=|<[\\/]?' + ZWSP + '?tool_call>')

function defaultMakeId(k) {
  return 'tgrepair_' + Date.now().toString(36) + '_' + k
}

export function toolsOf(options) {
  const empty = { names: [], schemas: {} }
  const tools = options && options.tools
  if (!Array.isArray(tools)) return empty
  const names = []
  const schemas = {}
  for (const tool of tools) {
    if (!tool || typeof tool.name !== 'string') continue
    names.push(tool.name)
    schemas[tool.name] = tool.parameters || {}
  }
  return { names, schemas }
}

export async function* repairStream(source, opts = {}) {
  const options = opts && typeof opts === 'object' ? opts : {}
  const toolNames = Array.isArray(options.toolNames) ? options.toolNames : []
  const schemas = options.schemas && typeof options.schemas === 'object' ? options.schemas : {}
  const makeId = typeof options.makeId === 'function' ? options.makeId : defaultMakeId
  const onRepair = typeof options.onRepair === 'function' ? options.onRepair : null

  const acc = new Map() // index -> accumulated text of open text blocks
  let held = null // array of chunks held back from the trigger delta on
  let heldIndex = -1
  let heldText = ''
  let highest = -1
  let structured = false

  const note = (chunk) => {
    if (chunk && typeof chunk.index === 'number' && chunk.index > highest) highest = chunk.index
    if (chunk && chunk.type === 'block-start' && chunk.blockType === 'tool-call') structured = true
  }

  for await (const chunk of source) {
    note(chunk)
    const type = chunk && chunk.type

    if (type === 'finish') {
      if (held === null) {
        yield chunk
        return
      }
      let full = heldText
      let heldEnd = null
      for (const c of held) {
        if (c && c.type === 'block-end' && c.index === heldIndex) {
          heldEnd = c
          if (c.block && typeof c.block.text === 'string') full = c.block.text
        }
      }
      let parsed = { calls: [], rest: full }
      try {
        parsed = parseTextToolCalls(full, toolNames, schemas)
      } catch {
        parsed = { calls: [], rest: full }
      }
      const calls = parsed && Array.isArray(parsed.calls) ? parsed.calls : []
      const kind = chunk.reason && chunk.reason.kind
      if (structured || kind === 'error' || kind === 'aborted' || calls.length === 0) {
        for (const c of held) yield c
        yield chunk
        return
      }

      const rest = parsed && typeof parsed.rest === 'string' ? parsed.rest : full
      for (const c of held) {
        if (c && c.type === 'text-delta' && c.index === heldIndex) continue
        if (c && c.type === 'usage') continue // kept back; replayed after the new tool-call blocks
        if (c === heldEnd) {
          yield { type: 'block-end', index: heldIndex, block: { type: 'text', text: rest } }
          continue
        }
        yield c
      }
      for (const call of calls) {
        if (onRepair) {
          try {
            onRepair({ tool: call && call.name })
          } catch {
            /* a throwing callback is ignored */
          }
        }
      }
      for (let k = 0; k < calls.length; k += 1) {
        const call = calls[k] || {}
        const index = highest + 1 + k
        const id = makeId(k)
        const args = JSON.stringify(call.args || {})
        yield { type: 'block-start', index, blockType: 'tool-call' }
        yield { type: 'tool-call-delta', index, id, name: call.name, argumentsDelta: args }
        yield { type: 'block-end', index, block: { type: 'tool-call', id, name: call.name, arguments: args } }
      }
      const usage = held.find((c) => c && c.type === 'usage')
      if (usage) yield usage
      yield { type: 'finish', reason: { kind: 'tool-calls' } }
      return
    }

    if (held !== null) {
      held.push(chunk)
      if (type === 'text-delta' && chunk.index === heldIndex) heldText += typeof chunk.text === 'string' ? chunk.text : ''
      continue
    }

    if (type === 'text-delta') {
      const so_far = (acc.get(chunk.index) || '') + (typeof chunk.text === 'string' ? chunk.text : '')
      acc.set(chunk.index, so_far)
      if (TRIGGER.test(so_far)) {
        held = []
        heldIndex = chunk.index
        heldText = so_far
        held.push(chunk)
        continue
      }
    }
    yield chunk
  }

  if (held !== null) for (const c of held) yield c
}

async function* fromArray(chunks) {
  for (const c of chunks) yield c
}

function textStream(pieces, extra = []) {
  const out = [{ type: 'block-start', index: 0, blockType: 'text' }]
  for (const p of pieces) out.push({ type: 'text-delta', index: 0, text: p })
  out.push({ type: 'block-end', index: 0, block: { type: 'text', text: pieces.join('') } })
  return out.concat(extra, [{ type: 'usage', usage: { inputTokens: 1, outputTokens: 2 } }, { type: 'finish', reason: { kind: 'stop' } }])
}

async function collect(it) {
  const out = []
  for await (const c of it) out.push(c)
  return out
}

export async function selftest() {
  const call = 'I will look.\n\n<function=search_capabilities>\n<parameter=query>\nrestore_head\n</parameter>\n</function>\n</tool_call>'
  const opts = { toolNames: ['search_capabilities'], schemas: {}, makeId: (k) => 'id' + k }
  const checks = [
    ['plain_text_unchanged', async () => {
      const src = textStream(['hello ', 'world'])
      return JSON.stringify(await collect(repairStream(fromArray(src), opts))) === JSON.stringify(src)
    }],
    ['text_call_becomes_tool_call', async () => {
      const out = await collect(repairStream(fromArray(textStream([call.slice(0, 20), call.slice(20)])), opts))
      const end = out.find((c) => c.type === 'block-end' && c.block.type === 'tool-call')
      return Boolean(end) && end.block.name === 'search_capabilities' && JSON.parse(end.block.arguments).query === 'restore_head' && end.block.id === 'id0'
    }],
    ['finish_becomes_tool_calls', async () => {
      const out = await collect(repairStream(fromArray(textStream([call])), opts))
      return out[out.length - 1].type === 'finish' && out[out.length - 1].reason.kind === 'tool-calls'
    }],
    ['text_block_keeps_only_rest', async () => {
      const out = await collect(repairStream(fromArray(textStream([call])), opts))
      const end = out.find((c) => c.type === 'block-end' && c.index === 0)
      return end.block.text.startsWith('I will look.') && !end.block.text.includes('<function=')
    }],
    ['usage_once_before_finish', async () => {
      const out = await collect(repairStream(fromArray(textStream([call])), opts))
      const u = out.map((c, i) => (c.type === 'usage' ? i : -1)).filter((i) => i >= 0)
      return u.length === 1 && u[0] === out.length - 2
    }],
    ['unknown_tool_unchanged', async () => {
      const src = textStream([call])
      const out = await collect(repairStream(fromArray(src), { toolNames: ['other'], schemas: {} }))
      return JSON.stringify(out) === JSON.stringify(src)
    }],
    ['structured_call_present_unchanged', async () => {
      const tc = [{ type: 'block-start', index: 1, blockType: 'tool-call' }, { type: 'tool-call-delta', index: 1, id: 'x', name: 'search_capabilities', argumentsDelta: '{}' }, { type: 'block-end', index: 1, block: { type: 'tool-call', id: 'x', name: 'search_capabilities', arguments: '{}' } }]
      const src = textStream([call], tc)
      return JSON.stringify(await collect(repairStream(fromArray(src), opts))) === JSON.stringify(src)
    }],
    ['on_repair_called_per_call', async () => {
      const seen = []
      await collect(repairStream(fromArray(textStream([call])), Object.assign({}, opts, { onRepair: (e) => seen.push(e.tool) })))
      return seen.length === 1 && seen[0] === 'search_capabilities'
    }],
    ['tools_of_options', async () => {
      const t = toolsOf({ tools: [{ name: 'a', description: '', parameters: { properties: { x: { type: 'integer' } } } }] })
      return t.names.length === 1 && t.names[0] === 'a' && t.schemas.a.properties.x.type === 'integer' && toolsOf(undefined).names.length === 0
    }],
  ]
  let passed = 0
  for (const [name, fn] of checks) {
    let ok = false
    let label = name
    try {
      ok = Boolean(await fn())
    } catch (err) {
      label = name + ' (raised ' + (err && err.name) + ': ' + (err && err.message) + ')'
    }
    if (ok) passed += 1
    console.log((ok ? 'ok   ' : 'FAIL ') + label)
  }
  const failed = checks.length - passed
  console.log('text_toolcall_repair selftest: ' + checks.length + ' checks, ' + passed + ' passed, ' + failed + ' failed')
  return { checks: checks.length, passed, failed }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv.includes('--selftest')) {
  selftest().then((r) => process.exit(r.failed === 0 && r.checks > 0 ? 0 : 1))
}
