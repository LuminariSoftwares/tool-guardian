# @studio: QA | Overseer probe for tool-guardian's tg_spill.py -- written from the CONTRACT, not from the module
# @kind: cli
# @called_by: human | delegate_task acceptance (tgp2_acc_spill.bat)
"""py probe_tg_spill.py [dir-holding-tg_spill.py]   -> last line: probe_tg_spill: N checks, N passed, M failed"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "."))
import tg_spill as S  # noqa: E402

TEXT = "".join("row %04d of the original\n" % i for i in range(1, 1001))


def c_save_id_is_content_hash_and_idempotent():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d)
        a, b = st.save(TEXT, tool="bash", rule="shell"), st.save(TEXT, tool="bash", rule="shell")
        want = "sp_" + hashlib.sha256(TEXT.encode("utf-8")).hexdigest()[:12]
        files = sorted(p.name for p in Path(d).iterdir())
        return (a["id"] == want == b["id"] and a["chars"] == len(TEXT) and Path(a["path"]).is_file()
                and files == [want + ".json", want + ".txt"])


def c_get_window():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d)
        i = st.save(TEXT)["id"]
        r = st.get(i, start_line=500, max_lines=3)
        return (r["ok"] is True and r["total_lines"] == 1000 and r["start_line"] == 500
                and r["returned_lines"] == 3
                and r["text"] == "row 0500 of the original\nrow 0501 of the original\nrow 0502 of the original\n")


def c_get_default_is_bounded():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d)
        r = st.get(st.save(TEXT)["id"])
        return r["ok"] is True and r["start_line"] == 1 and r["returned_lines"] == 400 and r["truncated"] is True


def c_grep_prefixes_line_numbers():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d)
        r = st.get(st.save(TEXT)["id"], grep=r"ROW 09\d5 ")
        return (r["ok"] is True and r["returned_lines"] == 10
                and r["text"].splitlines()[0] == "L905: row 0905 of the original")


def c_bad_regex_is_refused():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d)
        r = st.get(st.save(TEXT)["id"], grep="([")
        return r["ok"] is False and "regex" in r["error"].lower()


def c_traversal_and_unknown_ids_are_refused():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d)
        st.save(TEXT)
        bad = [st.get(x) for x in ("../etc", "sp_zzzzzzzzzzzz", "sp_0123456789ab", "", "sp_0123456789abcdef")]
        return all(r["ok"] is False and r.get("error") for r in bad)


def c_prune_keeps_newest():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d, keep=100)
        ids = []
        for n in range(6):
            r = st.save("spill number %d\n" % n)
            ids.append(r["id"])
            os.utime(r["path"], (1000 + n, 1000 + n))
        removed = S.SpillStore(d, keep=3).prune()      # a second store over the same directory
        left = sorted(p.stem for p in Path(d).glob("*.txt"))
        return removed == 3 and left == sorted(ids[3:]) and len(list(Path(d).glob("*.json"))) == 3


def c_default_root_env_then_home():
    saved = os.environ.pop("TOOL_GUARDIAN_SPILL_DIR", None)
    try:
        home_default = S.default_root()
        os.environ["TOOL_GUARDIAN_SPILL_DIR"] = os.path.join(tempfile.gettempdir(), "tgspill-probe")
        env_default = S.default_root()
    finally:
        os.environ.pop("TOOL_GUARDIAN_SPILL_DIR", None)
        if saved is not None:
            os.environ["TOOL_GUARDIAN_SPILL_DIR"] = saved
    return (Path(home_default) == Path.home() / ".tool-guardian" / "spill"
            and Path(env_default).name == "tgspill-probe")


def c_utf8_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        st = S.SpillStore(d)
        t = "caf\u00e9 \u4e2d\u6587 line\nsecond\n"
        r = st.get(st.save(t)["id"])
        return r["text"] == t and r["truncated"] is False


def c_notice_names_the_tool_call():
    n = S.notice("sp_0123456789ab", 5000, 900)
    return ("sp_0123456789ab" in n and "retrieve_spill" in n and "5000" in n and "\n" not in n.strip())


CHECKS = [
    ("save_id_is_content_hash_and_idempotent", c_save_id_is_content_hash_and_idempotent),
    ("get_window", c_get_window), ("get_default_is_bounded", c_get_default_is_bounded),
    ("grep_prefixes_line_numbers", c_grep_prefixes_line_numbers), ("bad_regex_is_refused", c_bad_regex_is_refused),
    ("traversal_and_unknown_ids_are_refused", c_traversal_and_unknown_ids_are_refused),
    ("prune_keeps_newest", c_prune_keeps_newest), ("default_root_env_then_home", c_default_root_env_then_home),
    ("utf8_roundtrip", c_utf8_roundtrip), ("notice_names_the_tool_call", c_notice_names_the_tool_call),
]


def main():
    passed = 0
    for name, fn in CHECKS:
        try:
            ok, why = fn() is True, ""
        except Exception as exc:  # noqa: BLE001
            ok, why = False, " (%s: %s)" % (type(exc).__name__, exc)
        print("  %s %s%s" % ("ok  " if ok else "FAIL", name, why))
        passed += 1 if ok else 0
    total = len(CHECKS)
    print("probe_tg_spill: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
