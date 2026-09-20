# @studio: QA | Overseer probe for tool-guardian's tg_ladder.py -- written from the CONTRACT, not from the module
# @kind: cli
# @called_by: human | delegate_task acceptance (tgp2_acc_ladder.bat)
"""py probe_tg_ladder.py [dir-holding-tg_ladder.py]   -> last line: probe_tg_ladder: N checks, N passed, M failed"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "."))
import tg_ladder as L  # noqa: E402

KEYS = {"text", "rule", "lossy", "original_chars", "final_chars"}


def c_exempt():
    t = "x" * 50000
    r = L.compress(t, tool="read")
    return set(r) == KEYS and r["text"] == t and r["rule"] == "exempt" and r["lossy"] is False


def c_small_untouched():
    t = "hello \x1b[31mworld\x1b[0m\n" * 20            # under 1200: byte-identical, ANSI and all
    r = L.compress(t, tool="bash")
    return r["text"] == t and r["rule"] == "small" and r["lossy"] is False


def c_error_summary():
    t = "".join("E%04d " % i for i in range(1000))      # 6000 chars
    r = L.compress(t, tool="call_tool", is_error=True)
    return (r["rule"] == "error" and r["lossy"] is True and r["text"].startswith(t[:200])
            and r["text"].endswith(t[-100:]) and len(r["text"]) <= 380
            and "5700 chars omitted" in r["text"])


def c_short_error_untouched():
    t = "boom: no such file"
    r = L.compress(t, tool="bash", is_error=True)
    return r["text"] == t and r["lossy"] is False


def c_clean_is_lossless():
    t = ("\x1b[31mred start\x1b[0m\n" + "same line here\n" * 50 + "\n\n\n\n" + "tail line\n"
         + "".join("u%04d unique\n" % i for i in range(60)))
    r = L.compress(t, tool="bash")
    x = r["text"]
    return (r["rule"] == "clean" and r["lossy"] is False and "\x1b" not in x
            and x.count("same line here") == 1 and "[x50]" in x and "\n\n\n" not in x
            and "u0059 unique" in x and "red start" in x)


def _items(n):
    return [{"id": i, "name": "item-%d" % i, "payload": "p" * 40} for i in range(n)]


def c_json_array():
    items = _items(400)
    r = L.compress(json.dumps(items), tool="call_tool")
    o = json.loads(r["text"])
    return (r["rule"] == "json_array" and r["lossy"] is True and o["_compressed"] == "json_array"
            and o["total_items"] == 400 and o["first"] == items[:5] and o["last"] == items[-5:]
            and o["omitted"] == 390 and o["keys"] == ["id", "name", "payload"]
            and r["final_chars"] < 2500)


def c_json_few_big_items_is_not_json_array_and_is_bounded():
    t = json.dumps([{"blob": "b" * 2000} for _ in range(8)])    # one 16k line, 8 items
    r = L.compress(t, tool="call_tool")
    return r["rule"] != "json_array" and r["lossy"] is True and r["final_chars"] < 8000


def c_csv():
    rows = ["id,name,value"] + ["%d,name%d,%d" % (i, i, i * 7) for i in range(1, 1001)]
    r = L.compress("\n".join(rows) + "\n", tool="call_tool")
    x = r["text"]
    return (r["rule"] == "csv" and r["lossy"] is True and x.splitlines()[0] == "id,name,value"
            and "[csv: 1000 rows x 3 cols]" in x and "1,name1,7" in x and "1000,name1000,7000" in x
            and "500,name500,3500" not in x and "rows omitted" in x)


def _shell(n=3000, marker_mid=True):
    lines = ["line %05d output text" % i for i in range(1, n + 1)]
    if marker_mid:
        lines[1499] = "[stderr] something in the middle"
    return "\n".join(lines) + "\n[exit code: 1]\n"


def c_shell():
    r = L.compress(_shell(), tool="bash")
    x = r["text"]
    sampled = re.findall(r"(?m)^L(\d+): line \d{5} output text$", x)
    return (r["rule"] == "shell" and r["lossy"] is True and "line 00001 output text" in x
            and "line 03000 output text" in x and "[exit code: 1]" in x
            and "[stderr] something in the middle" in x and len(sampled) == 20
            and "lines omitted" in x and r["final_chars"] < 8000
            and all(40 < int(n) <= 2961 for n in sampled))


def c_sampled_for_non_shell_tool():
    r = L.compress(_shell(marker_mid=False).replace("[exit code: 1]\n", ""), tool="web_fetch")
    return r["rule"] == "sampled" and r["lossy"] is True and r["final_chars"] < 8000


def c_cfg_override():
    r = L.compress(_shell(), tool="bash", cfg={"shell_sample_lines": 5})
    return len(re.findall(r"(?m)^L\d+: line", r["text"])) == 5


def c_diff():
    before = [" ctx %03d" % i for i in range(100)]
    after = [" ctx %03d" % i for i in range(100, 200)]
    t = "\n".join(["diff --git a/f.py b/f.py", "--- a/f.py", "+++ b/f.py", "@@ -1,201 +1,201 @@"]
                  + before + ["-old value", "+new value"] + after) + "\n"
    r = L.compress(t, tool="bash")
    x = r["text"]
    return (r["rule"] == "diff" and r["lossy"] is True and "-old value" in x and "+new value" in x
            and "@@ -1,201 +1,201 @@" in x and "diff --git a/f.py b/f.py" in x
            and all((" ctx %03d" % i) in x for i in (98, 99, 100, 101))
            and " ctx 050" not in x and " ctx 097" not in x and "context lines" in x)


def c_deterministic_and_counts():
    a, b = L.compress(_shell(), tool="bash"), L.compress(_shell(), tool="bash")
    return (a == b and a["original_chars"] == len(_shell()) and a["final_chars"] == len(a["text"]))


def c_mid_size_plain_text_passes():
    t = "".join("plain unique sentence number %04d.\n" % i for i in range(100))   # ~3.4k, nothing to clean
    r = L.compress(t, tool="web_fetch")
    return r["text"] == t and r["rule"] == "pass" and r["lossy"] is False


def c_defaults_exposed():
    d = L.DEFAULTS
    return (d["compact_above_chars"] == 1200 and d["error_summary_chars"] == 300
            and d["struct_min_chars"] == 10000 and d["shell_min_chars"] == 8000
            and "read" in d["exempt_tools"] and "bash" in d["shell_tools"])


CHECKS = [
    ("exempt", c_exempt), ("small_untouched", c_small_untouched), ("error_summary", c_error_summary),
    ("short_error_untouched", c_short_error_untouched), ("clean_is_lossless", c_clean_is_lossless),
    ("json_array", c_json_array),
    ("json_few_big_items_is_not_json_array_and_is_bounded", c_json_few_big_items_is_not_json_array_and_is_bounded),
    ("csv", c_csv), ("shell", c_shell), ("sampled_for_non_shell_tool", c_sampled_for_non_shell_tool),
    ("cfg_override", c_cfg_override), ("diff", c_diff),
    ("deterministic_and_counts", c_deterministic_and_counts),
    ("mid_size_plain_text_passes", c_mid_size_plain_text_passes), ("defaults_exposed", c_defaults_exposed),
]


def main():
    passed = 0
    for name, fn in CHECKS:
        try:
            ok = fn() is True
            why = ""
        except Exception as exc:  # noqa: BLE001
            ok, why = False, " (%s: %s)" % (type(exc).__name__, exc)
        print("  %s %s%s" % ("ok  " if ok else "FAIL", name, why))
        passed += 1 if ok else 0
    total = len(CHECKS)
    print("probe_tg_ladder: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
