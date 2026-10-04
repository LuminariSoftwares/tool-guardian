/**
 * text_toolcalls.js
 *
 * Pure, dependency-free module that recovers tool calls that a local model wrote
 * as PLAIN TEXT instead of emitting them as real tool calls, e.g.
 *
 *     I'll search for it.
 *
 *     <tool_call>
 *     <function=search_capabilities>
 *     <parameter=query>
 *     restore_head
 *     </parameter>
 *     </function>
 *     </tool_call>
 *
 * Exports:
 *   parseTextToolCalls(text, toolNames, schemas) -> {calls, rest, malformed}
 *   selftest()                                   -> {checks, passed, failed}
 *
 * ES module, Node 20+. This file has no imports at all, so importing it can
 * never throw (not even when process.argv[1] is undefined).
 */

/* ------------------------------------------------------------------ *
 * constants
 * ------------------------------------------------------------------ */

// Zero-width / BOM characters that some models slip in between "<" and
// "tool_call". Built from code points so this source file stays pure ASCII.
const ZERO_WIDTH = String.fromCharCode(0x200b, 0x200c, 0x200d, 0xfeff);

// "<tool_call>" and "</tool_call>", with or without a stray zero-width char.
const TOOL_CALL_TAG = new RegExp("</?[" + ZERO_WIDTH + "]?tool_call>", "g");

const CLOSE_FUNCTION = "</function>";
const OPEN_FUNCTION_RE = /<function=([^>\s]+)>/g;
const PARAMETER_RE = /<parameter=([^>]*)>([\s\S]*?)<\/parameter>/g;

const JSON_TYPES = new Set(["number", "integer", "boolean", "object", "array"]);

/* ------------------------------------------------------------------ *
 * small helpers
 * ------------------------------------------------------------------ */

/** Coerce anything to a string without ever throwing. null/undefined -> "". */
function toText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return String(value);
  } catch {
    return "";
  }
}

/** Normalize the tool-name list into a Set. Never throws. */
function toNameSet(toolNames) {
  const set = new Set();
  if (toolNames === null || toolNames === undefined) return set;
  let list = toolNames;
  if (typeof list === "string") list = [list];
  if (typeof list.length === "number" && typeof list !== "function") {
    for (let i = 0; i < list.length; i++) {
      const n = list[i];
      if (typeof n === "string") set.add(n);
      else if (n !== null && n !== undefined) set.add(String(n));
    }
  }
  return set;
}

/** schemas -> a plain object, or {} when unusable. */
function toSchemaMap(schemas) {
  if (schemas !== null && typeof schemas === "object") return schemas;
  return {};
}

/* ------------------------------------------------------------------ *
 * fenced code blocks
 * ------------------------------------------------------------------ */

/**
 * Ranges [start, end) covered by triple-backtick fences. A ``` that is never
 * closed runs to the end of the text. Never throws.
 */
function findFenceRanges(text) {
  const ranges = [];
  let i = text.indexOf("```");
  let open = -1;
  while (i !== -1) {
    if (open === -1) {
      open = i;
    } else {
      ranges.push([open, i + 3]);
      open = -1;
    }
    i = text.indexOf("```", i + 3);
  }
  if (open !== -1) ranges.push([open, text.length]);
  return ranges;
}

/**
 * Split [0, length) into alternating {fenced} / {plain} segments so that
 * fenced content can be copied through byte-for-byte.
 */
function splitSegments(length, fenceRanges) {
  const segments = [];
  let cursor = 0;
  for (const range of fenceRanges) {
    const start = range[0];
    const end = range[1];
    if (start > cursor) segments.push({ start: cursor, end: start, fenced: false });
    segments.push({ start, end, fenced: true });
    cursor = end;
  }
  if (cursor < length) segments.push({ start: cursor, end: length, fenced: false });
  if (segments.length === 0) segments.push({ start: 0, end: length, fenced: false });
  return segments;
}

/* ------------------------------------------------------------------ *
 * parameter parsing / coercion
 * ------------------------------------------------------------------ */

/** Remove exactly ONE leading and ONE trailing newline (LF or CRLF). Nothing else. */
function trimOneNewline(value) {
  let v = value;
  if (v.startsWith("\r\n")) v = v.slice(2);
  else if (v.startsWith("\n")) v = v.slice(1);
  if (v.endsWith("\r\n")) v = v.slice(0, -2);
  else if (v.endsWith("\n")) v = v.slice(0, -1);
  return v;
}

/** The declared JSON type of property `key`, or undefined when not declared. */
function declaredType(schema, key) {
  if (schema === null || typeof schema !== "object") return undefined;
  const props = schema.properties;
  if (props === null || typeof props !== "object") return undefined;
  const prop = props[key];
  if (prop === null || typeof prop !== "object") return undefined;
  return typeof prop.type === "string" ? prop.type : undefined;
}

/** Coerce the (already newline-trimmed) raw value using its declared type. */
function coerceValue(raw, type) {
  if (!JSON_TYPES.has(type)) return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/** Extract {args} from one `<function=...>...</function>` block text. */
function parseArgs(blockText, schema) {
  const args = {};
  let m;
  PARAMETER_RE.lastIndex = 0;
  while ((m = PARAMETER_RE.exec(blockText)) !== null) {
    const key = m[1];
    const raw = trimOneNewline(m[2]);
    args[key] = coerceValue(raw, declaredType(schema, key));
  }
  return args;
}

/* ------------------------------------------------------------------ *
 * the parser
 * ------------------------------------------------------------------ */

function stripTags(segment) {
  TOOL_CALL_TAG.lastIndex = 0;
  return segment.replace(TOOL_CALL_TAG, "");
}

/**
 * Scan one unfenced segment: pull out every `<function=NAME>...</function>`
 * block in order, appending calls / malformed entries and returning the
 * remaining text with those blocks and any tool_call tags removed.
 */
function processSegment(segment, nameSet, schemaMap, calls, malformed) {
  let out = "";
  let last = 0;
  let m;
  OPEN_FUNCTION_RE.lastIndex = 0;
  while ((m = OPEN_FUNCTION_RE.exec(segment)) !== null) {
    const name = m[1];
    const closeIdx = segment.indexOf(CLOSE_FUNCTION, OPEN_FUNCTION_RE.lastIndex);
    if (closeIdx === -1) break; // unterminated block: leave the tail untouched
    const end = closeIdx + CLOSE_FUNCTION.length;
    const blockText = segment.slice(m.index, end);
    out += segment.slice(last, m.index);
    last = end;
    OPEN_FUNCTION_RE.lastIndex = end;
    if (nameSet.has(name)) {
      calls.push({ name, args: parseArgs(blockText, schemaMap[name]) });
    } else {
      malformed.push(blockText);
    }
  }
  out += segment.slice(last);
  return stripTags(out);
}

/**
 * @param {string} text
 * @param {string[]} toolNames  names that are allowed to become calls
 * @param {object} schemas      name -> {properties: {key: {type}}}
 * @returns {{calls: {name:string,args:object}[], rest: string, malformed: string[]}}
 */
export function parseTextToolCalls(text, toolNames, schemas) {
  const src = toText(text);
  const nameSet = toNameSet(toolNames);
  const schemaMap = toSchemaMap(schemas);

  const calls = [];
  const malformed = [];

  try {
    let rest = "";
    const segments = splitSegments(src.length, findFenceRanges(src));
    for (const seg of segments) {
      const piece = src.slice(seg.start, seg.end);
      rest += seg.fenced
        ? piece
        : processSegment(piece, nameSet, schemaMap, calls, malformed);
    }
    return { calls, rest, malformed };
  } catch {
    // "never throws" is part of the contract
    return { calls, rest: src, malformed };
  }
}

/* ------------------------------------------------------------------ *
 * selftest
 * ------------------------------------------------------------------ */

const CHECKS = [
  [
    "one_call",
    () =>
      parseTextToolCalls(
        "<function=a>\n<parameter=x>\n1\n</parameter>\n</function>",
        ["a"],
        {},
      ).calls[0].args.x === "1",
  ],
  [
    "integer_typed",
    () =>
      parseTextToolCalls("<function=a><parameter=x>7</parameter></function>", ["a"], {
        a: { properties: { x: { type: "integer" } } },
      }).calls[0].args.x === 7,
  ],
  [
    "unknown_name",
    () => parseTextToolCalls("<function=zz></function>", ["a"], {}).malformed.length === 1,
  ],
  [
    "fenced_ignored",
    () => parseTextToolCalls("```\n<function=a></function>\n```", ["a"], {}).calls.length === 0,
  ],
  [
    "tag_stripped",
    () =>
      !parseTextToolCalls("<function=a></function>\n</tool_call>", ["a"], {}).rest.includes(
        "</tool_call>",
      ),
  ],
  ["plain_text", () => parseTextToolCalls("hello", ["a"], {}).rest === "hello"],
];

export function selftest() {
  const log = (line) => {
    if (typeof console !== "undefined" && console && typeof console.log === "function") {
      console.log(line);
    }
  };

  let passed = 0;
  let failed = 0;
  for (const entry of CHECKS) {
    const name = Array.isArray(entry) ? entry[0] : String(entry);
    const fn = Array.isArray(entry) ? entry[1] : null;
    let ok = false;
    try {
      ok = typeof fn === "function" ? !!fn() : false;
    } catch {
      ok = false;
    }
    if (ok) {
      passed++;
      log("ok   " + name);
    } else {
      failed++;
      log("FAIL " + name);
    }
  }

  const checks = passed + failed;
  log(`text_toolcalls selftest: ${checks} checks, ${passed} passed, ${failed} failed`);
  return { checks, passed, failed };
}

/* ------------------------------------------------------------------ *
 * direct-run detection
 * ------------------------------------------------------------------ */

function isDirectRun() {
  try {
    if (typeof process === "undefined" || !process.argv) return false;
    const argv1 = process.argv[1];
    if (!argv1) return false;
    if (
      typeof import.meta.url === "string" &&
      import.meta.url === new URL("file:///" + String(argv1).replace(/\\/g, "/")).href
    ) {
      return true;
    }
    return import.meta.url === new URL(String(argv1).replace(/\\/g, "/"), "file:///").href;
  } catch {
    return false;
  }
}

if (isDirectRun()) {
  let result = { checks: 0, passed: 0, failed: 0 };
  try {
    result = selftest();
  } catch {
    result = { checks: 0, passed: 0, failed: 1 };
  }
  if (typeof process !== "undefined" && typeof process.exit === "function") {
    process.exit(result.failed === 0 && result.checks > 0 ? 0 : 1);
  }
}
