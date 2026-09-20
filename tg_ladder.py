# @studio: Guardian | tool-guardian output ladder -- deterministic compression of tool results by type
# @kind: library
# @called_by: tool_guardian.Router.handle | modules/tg_bridge.py op_ladder
"""py tg_ladder.py --in IN.json --out OUT.json      (IN: {"text","tool","is_error","cfg"})
py tg_ladder.py --selftest

The output ladder: a PURE, DETERMINISTIC compressor for tool results. No I/O in
compress(), no clock, no randomness, no environment -- the same input always gives
the same output, so provider prompt caches keep hitting. The CALLER archives the
original whenever `lossy` is True (tool_guardian.Router.shape does).

CONTRACT
    compress(text, tool="", is_error=False, cfg=None)
        -> {"text", "rule", "lossy", "original_chars", "final_chars"}
    First matching rule wins:
      exempt      tool in exempt_tools                        unchanged
      error       is_error: over error_summary_chars -> head 2/3 + tail 1/3
      small       under compact_above_chars                   unchanged, byte for byte
      (everything else is CLEANED losslessly first: ANSI out, repeated lines -> "line  [xN]",
       blank runs -> one blank line; thresholds below are measured on the cleaned text)
      diff        unified diff: every change + diff_context_lines of context
      json_array  >= struct_min_chars, a JSON list of more than 2K items
      csv         >= struct_min_chars, a consistent , or TAB delimiter
      shell       >= shell_min_chars and tool in shell_tools: head + samples + tail
      sampled     >= shell_min_chars, any other tool: the same algorithm
      clean/pass  cleaning changed it / nothing to do

History: written from the P19 contract after one local-model attempt and two ox attempts
(2026-09-19) each landed 10-11 of the 15 contract-probe checks; the cleaning loop and the
JSON-array shape follow those attempts. MIT licensed.
"""
from __future__ import annotations

import json
import re
import sys

DEFAULTS = {
    "compact_above_chars": 1200,   # below this: untouched
    "error_summary_chars": 300,
    "struct_min_chars": 10000,     # JSON array / CSV rules start here
    "shell_min_chars": 8000,       # shell / sampled rules start here
    "shell_head_lines": 40,
    "shell_tail_lines": 40,
    "shell_sample_lines": 20,
    "struct_keep_items": 5,
    "diff_context_lines": 2,
    "char_head": 3000,             # one-giant-line fallback
    "char_tail": 1500,
    "exempt_tools": ["read", "read_image", "read_skill", "describe_tool", "retrieve_spill"],
    "shell_tools": ["bash", "pwsh", "run_code"],
}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
MARKER_RE = re.compile(r"^\[(exit code|killed by signal|stderr|sandbox|timeout)[^\]]*\]")
DIFF_RE = re.compile(r"(?m)^diff --git ")
DIFF_KEEP = ("diff --git ", "index ", "--- ", "+++ ", "@@")


def clean(text: str) -> str:
    """Lossless for a reader: every dropped repeat is counted; what goes is ANSI colour,
    CR line endings and trailing padding. PowerShell pads table rows to the console width
    and ends them CRLF -- on a real `dir` of 360 files that padding was most of the text,
    and a "\r" on every blank line defeated the blank-run collapse (seen live 2026-09-19)."""
    # In a unified diff a lone " " IS a (blank) context line, so only the CR goes there.
    pad = "\r" if DIFF_RE.search(text) else " \t\r"
    lines = [ln.rstrip(pad) for ln in ANSI_RE.sub("", text).split("\n")]
    out, i = [], 0
    while i < len(lines):
        j = i + 1
        while j < len(lines) and lines[j] == lines[i]:
            j += 1
        run = j - i
        if lines[i] == "":
            # the final "" is the trailing newline, not a blank line: keep it as it is
            out.append("")
        elif run > 1:
            out.append("%s  [x%d]" % (lines[i], run))
        else:
            out.append(lines[i])
        i = j
    return "\n".join(out)


def _diff(cleaned: str, ctx: int):
    lines = cleaned.split("\n")
    changes = [i for i, ln in enumerate(lines)
               if ln[:1] in "+-" and ln[:1] and not ln.startswith(("+++ ", "--- "))]
    keep = set()
    for i, ln in enumerate(lines):
        if ln.startswith(DIFF_KEEP) or i in changes or (ln == "" and i == len(lines) - 1):
            keep.add(i)
    for c in changes:
        keep.update(range(max(0, c - ctx), min(len(lines), c + ctx + 1)))
    out, dropped, i = [], 0, 0
    while i < len(lines):
        if i in keep:
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and j not in keep:
            j += 1
        out.append("[... %d context lines ...]" % (j - i))
        dropped += j - i
        i = j
    return "\n".join(out), dropped > 0


def _json_array(cleaned: str, keep: int):
    try:
        data = json.loads(cleaned)
    except ValueError:
        return None
    if not isinstance(data, list) or len(data) <= 2 * keep:
        return None
    keys = sorted({k for item in data if isinstance(item, dict) for k in item})
    return json.dumps({"_compressed": "json_array", "total_items": len(data), "keys": keys,
                       "first": data[:keep], "last": data[-keep:],
                       "omitted": len(data) - 2 * keep}, ensure_ascii=False)


def _csv(cleaned: str, keep: int):
    rows = [ln for ln in cleaned.split("\n") if ln != ""]
    if len(rows) < 5:
        return None
    for delim in (",", "\t"):
        counts = {ln.count(delim) for ln in rows[:5]}
        if len(counts) == 1 and counts.pop() >= 1:
            break
    else:
        return None
    header, data = rows[0], rows[1:]
    if len(data) <= 2 * keep:
        return None
    return "\n".join([header, *data[:keep], "[... %d rows omitted ...]" % (len(data) - 2 * keep),
                      *data[-keep:], "[csv: %d rows x %d cols]" % (len(data), header.count(delim) + 1)])


def _sampled(cleaned: str, cfg: dict) -> str:
    lines = cleaned.splitlines()
    n, head, tail, want = len(lines), cfg["shell_head_lines"], cfg["shell_tail_lines"], cfg["shell_sample_lines"]
    text = cleaned
    if n > head + tail:
        mid = lines[head:n - tail]
        picks = {(i + 1) * len(mid) // (want + 1) for i in range(want)} if want > 0 else set()
        kept = sorted(picks | {k for k, ln in enumerate(mid) if MARKER_RE.match(ln)})
        out, cursor = list(lines[:head]), 0
        for k in kept:
            if k > cursor:
                out.append("[... %d lines omitted ...]" % (k - cursor))
            out.append(mid[k] if MARKER_RE.match(mid[k]) else "L%d: %s" % (head + k + 1, mid[k]))
            cursor = k + 1
        if len(mid) > cursor:
            out.append("[... %d lines omitted ...]" % (len(mid) - cursor))
        out.extend(lines[n - tail:])
        text = "\n".join(out) + ("\n" if cleaned.endswith("\n") else "")
    if len(text) >= cfg["shell_min_chars"]:       # one giant line, or few enormous ones
        a, b = cfg["char_head"], cfg["char_tail"]
        text = "%s\n[... %d chars omitted ...]\n%s" % (cleaned[:a], len(cleaned) - a - b, cleaned[-b:])
    return text


def compress(text: str, tool: str = "", is_error: bool = False, cfg: dict | None = None) -> dict:
    conf = dict(DEFAULTS)
    conf.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})

    def done(out: str, rule: str, lossy: bool) -> dict:
        return {"text": out, "rule": rule, "lossy": lossy,
                "original_chars": len(text), "final_chars": len(out)}

    if tool in conf["exempt_tools"]:
        return done(text, "exempt", False)
    if is_error:
        limit = conf["error_summary_chars"]
        if len(text) <= limit:
            return done(text, "error", False)
        head = limit * 2 // 3
        tail = limit - head
        return done("%s\n[... %d chars omitted ...]\n%s" % (text[:head], len(text) - limit, text[-tail:]),
                    "error", True)
    if len(text) < conf["compact_above_chars"]:
        return done(text, "small", False)

    cleaned = clean(text)
    if DIFF_RE.search(cleaned):
        out, dropped = _diff(cleaned, conf["diff_context_lines"])
        if dropped:
            return done(out, "diff", True)
    if len(cleaned) >= conf["struct_min_chars"]:
        out = _json_array(cleaned, conf["struct_keep_items"])
        if out is not None:
            return done(out, "json_array", True)
        out = _csv(cleaned, conf["struct_keep_items"])
        if out is not None:
            return done(out, "csv", True)
    if len(cleaned) >= conf["shell_min_chars"]:
        return done(_sampled(cleaned, conf), "shell" if tool in conf["shell_tools"] else "sampled", True)
    return done(cleaned, "clean", False) if cleaned != text else done(text, "pass", False)


# ------------------------------------------------------------- selftest -----

def _shell_text(n=3000):
    return "\n".join("line %05d output text" % i for i in range(1, n + 1)) + "\n[exit code: 1]\n"


def _t_exempt_tool_untouched():
    t = "x" * 50000
    r = compress(t, tool="read")
    return r["text"] == t and r["rule"] == "exempt" and r["lossy"] is False


def _t_error_over_300_is_head_tail():
    t = "".join("E%04d " % i for i in range(1000))
    r = compress(t, is_error=True)
    return (r["rule"] == "error" and r["lossy"] is True and r["text"].startswith(t[:200])
            and r["text"].endswith(t[-100:]) and "[... 5700 chars omitted ...]" in r["text"])


def _t_short_error_untouched():
    r = compress("boom: no such file", is_error=True)
    return r["text"] == "boom: no such file" and r["rule"] == "error" and r["lossy"] is False


def _t_small_is_byte_identical():
    t = "hello \x1b[31mworld\x1b[0m\n" * 20
    r = compress(t, tool="bash")
    return r["text"] == t and r["rule"] == "small"


def _t_clean_strips_ansi_and_counts_dupes():
    t = "\x1b[31mred\x1b[0m\n" + "same line\n" * 50 + "\n\n\n\n" + "".join("u%04d unique\n" % i for i in range(80))
    r = compress(t, tool="bash")
    return (r["rule"] == "clean" and r["lossy"] is False and "\x1b" not in r["text"]
            and "same line  [x50]" in r["text"] and "\n\n\n" not in r["text"] and r["text"].endswith("u0079 unique\n"))


def _t_clean_drops_crlf_and_console_padding():
    row = "-a----   9/19/2026   7:12 PM    1234 some_script.py"
    t = "".join("%s%03d%s\r\n" % (row, i, " " * 60) for i in range(40)) + "\r\n\r\n\r\n"
    r = compress(t, tool="pwsh")
    return (r["rule"] == "clean" and r["lossy"] is False and "\r" not in r["text"]
            and "  \n" not in r["text"] and "\n\n\n" not in r["text"]
            and (row + "039") in r["text"] and r["final_chars"] < len(t) / 2)


def _t_diff_keeps_changes_and_near_context():
    body = [" ctx %03d" % i for i in range(100)] + ["-old", "+new"] + [" ctx %03d" % i for i in range(100, 200)]
    t = "\n".join(["diff --git a/f b/f", "--- a/f", "+++ b/f", "@@ -1,201 +1,201 @@"] + body) + "\n"
    x = compress(t)["text"]
    return (all(s in x for s in ("-old", "+new", " ctx 098", " ctx 101", "[... 98 context lines ...]"))
            and " ctx 097" not in x and " ctx 102" not in x and compress(t)["rule"] == "diff")


def _t_json_array_first_last_keys():
    items = [{"id": i, "name": "n%d" % i, "pad": "p" * 40} for i in range(400)]
    o = json.loads(compress(json.dumps(items))["text"])
    return (o["total_items"] == 400 and o["first"] == items[:5] and o["last"] == items[-5:]
            and o["omitted"] == 390 and o["keys"] == ["id", "name", "pad"])


def _t_json_short_list_falls_through():
    r = compress(json.dumps([{"blob": "b" * 2000} for _ in range(8)]))
    return r["rule"] == "sampled" and r["lossy"] is True


def _t_csv_header_first_last_footer():
    rows = ["id,name,value"] + ["%d,n%d,%d" % (i, i, i * 7) for i in range(1, 1001)]
    x = compress("\n".join(rows) + "\n")["text"].splitlines()
    return (x[0] == "id,name,value" and x[1] == "1,n1,7" and x[6] == "[... 990 rows omitted ...]"
            and x[-2] == "1000,n1000,7000" and x[-1] == "[csv: 1000 rows x 3 cols]")


def _t_shell_head_samples_tail():
    r = compress(_shell_text(), tool="bash")
    samples = re.findall(r"(?m)^L(\d+): line (\d{5}) ", r["text"])
    return (r["rule"] == "shell" and len(samples) == 20 and all(int(a) == int(b) for a, b in samples)
            and samples[0][0] == "180" and "line 00040 " in r["text"] and "line 03000 " in r["text"]
            and r["text"].endswith("[exit code: 1]\n") and r["final_chars"] < 4000)


def _t_shell_mid_marker_is_kept():
    lines = _shell_text().split("\n")
    lines[1499] = "[stderr] mid-run warning"
    x = compress("\n".join(lines), tool="bash")["text"]
    return "\n[stderr] mid-run warning\n" in x and "L1500" not in x


def _t_sampled_for_other_tools():
    return compress(_shell_text(), tool="web_fetch")["rule"] == "sampled"


def _t_one_giant_line_char_fallback():
    t = "z" * 20000
    r = compress(t, tool="bash")
    return (r["text"] == "z" * 3000 + "\n[... 15500 chars omitted ...]\n" + "z" * 1500 and r["lossy"] is True)


def _t_same_input_same_output():
    a, b = compress(_shell_text(), tool="bash"), compress(_shell_text(), tool="bash")
    return a == b and a["original_chars"] == len(_shell_text()) and a["final_chars"] == len(a["text"])


def _t_cfg_overrides_and_ignores_unknown_keys():
    r = compress(_shell_text(), tool="bash", cfg={"shell_sample_lines": 5, "nonsense": 1})
    return len(re.findall(r"(?m)^L\d+: ", r["text"])) == 5


def _t_cli_in_out_roundtrip():
    import tempfile  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.json", Path(tmp) / "out.json"
        src.write_text(json.dumps({"text": _shell_text(), "tool": "bash"}), encoding="utf-8")
        code = main(["--in", str(src), "--out", str(dst)])
        return code == 0 and json.loads(dst.read_text(encoding="utf-8")) == compress(_shell_text(), tool="bash")


CHECKS = [
    ("exempt_tool_untouched", _t_exempt_tool_untouched),
    ("error_over_300_is_head_tail", _t_error_over_300_is_head_tail),
    ("short_error_untouched", _t_short_error_untouched),
    ("small_is_byte_identical", _t_small_is_byte_identical),
    ("clean_strips_ansi_and_counts_dupes", _t_clean_strips_ansi_and_counts_dupes),
    ("clean_drops_crlf_and_console_padding", _t_clean_drops_crlf_and_console_padding),
    ("diff_keeps_changes_and_near_context", _t_diff_keeps_changes_and_near_context),
    ("json_array_first_last_keys", _t_json_array_first_last_keys),
    ("json_short_list_falls_through", _t_json_short_list_falls_through),
    ("csv_header_first_last_footer", _t_csv_header_first_last_footer),
    ("shell_head_samples_tail", _t_shell_head_samples_tail),
    ("shell_mid_marker_is_kept", _t_shell_mid_marker_is_kept),
    ("sampled_for_other_tools", _t_sampled_for_other_tools),
    ("one_giant_line_char_fallback", _t_one_giant_line_char_fallback),
    ("same_input_same_output", _t_same_input_same_output),
    ("cfg_overrides_and_ignores_unknown_keys", _t_cfg_overrides_and_ignores_unknown_keys),
    ("cli_in_out_roundtrip", _t_cli_in_out_roundtrip),
]


def selftest() -> int:
    passed = 0
    for name, fn in CHECKS:
        try:
            ok = fn() is True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  FAIL %s (%s: %s)" % (name, type(exc).__name__, exc))
        else:
            print("  %s %s" % ("ok  " if ok else "FAIL", name))
        passed += 1 if ok else 0
    total = len(CHECKS)
    print("tg_ladder selftest: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if total > 0 and passed == total else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--in" not in argv or "--out" not in argv:
        print(__doc__.split("\n\n")[0])
        return 2
    with open(argv[argv.index("--in") + 1], encoding="utf-8") as fh:
        req = json.load(fh)
    out = compress(str(req.get("text", "")), tool=str(req.get("tool") or ""),
                   is_error=bool(req.get("is_error")), cfg=req.get("cfg") or None)
    with open(argv[argv.index("--out") + 1], "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
