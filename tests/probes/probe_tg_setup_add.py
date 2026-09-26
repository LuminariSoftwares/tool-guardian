# @studio: QA | Overseer probe for tool-guardian-setup add/remove/list -- written from the CONTRACT, not from the module
# @kind: cli
# @called_by: human | ox acceptance (tg-setup-add-v1)
"""py probe_tg_setup_add.py [dir-holding-tg_setup.py]   -> last line: probe_tg_setup_add: N checks, N passed, M failed

Every check runs tg_setup.py as a subprocess in a throwaway HOME/cwd with TOOL_GUARDIAN_CONFIG pointed
into that dir (or unset), so the real ~/.tool-guardian and PATH config are never read or written."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
SETUP = os.path.join(REPO, "tg_setup.py")
PY = os.path.basename(sys.executable).rsplit(".", 1)[0]   # "python" / "python3": on PATH in any sane shell
NOPE = "definitely-not-a-command-tg-probe"
RESTART = "restart your MCP client (or start a new DSH session) to load it"


class Box:
    def __init__(self, use_env=True):
        self.dir = tempfile.mkdtemp(prefix="tgprobe_")
        self.cfg = os.path.join(self.dir, "dsh", "tg_mcp.json")
        self.env = dict(os.environ)
        for k in ("TOOL_GUARDIAN_CONFIG", "TOOL_GUARDIAN_ENV", "PROBE_SET_VAR", "PROBE_UNSET_VAR"):
            self.env.pop(k, None)
        self.env["HOME"] = self.env["USERPROFILE"] = self.dir
        self.env["PYTHONIOENCODING"] = "utf-8"
        if use_env:
            self.env["TOOL_GUARDIAN_CONFIG"] = self.cfg

    def run(self, *args):
        p = subprocess.run([sys.executable, SETUP, *args], cwd=self.dir, env=self.env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        return p.returncode, p.stdout + p.stderr

    def write(self, obj, path=None):
        path = path or self.cfg
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def read(self, path=None):
        with open(path or self.cfg, encoding="utf-8") as fh:
            return json.load(fh)

    def backups(self, path=None):
        path = path or self.cfg
        d, base = os.path.dirname(path), os.path.basename(path)
        return [n for n in os.listdir(d) if n.startswith(base + ".bak")] if os.path.isdir(d) else []

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def c_add_writes_the_file_doctor_finds():
    b = Box()
    try:
        code, out = b.run("add", "git", "--", PY, "-m", "mcp_server_git", "--repo", ".")
        spec = b.read()["mcpServers"]["git"]
        return code == 0 and spec["command"] == PY and spec["args"] == ["-m", "mcp_server_git", "--repo", "."] \
            and not os.path.exists(os.path.join(b.dir, ".tool-guardian", "mcp.json"))
    finally:
        b.close()


def c_dash_args_after_separator_are_kept():
    b = Box()
    try:
        code, _ = b.run("add", "fs", "--", PY, "-y", "--flag", "/data")
        return code == 0 and b.read()["mcpServers"]["fs"]["args"] == ["-y", "--flag", "/data"]
    finally:
        b.close()


def c_env_and_description_are_written():
    b = Box()
    try:
        code, _ = b.run("add", "gh", "--env", "A=1", "--env", "B=two=2", "--description", "GitHub issues", "--", PY)
        spec = b.read()["mcpServers"]["gh"]
        return code == 0 and spec.get("env") == {"A": "1", "B": "two=2"} and spec.get("description") == "GitHub issues"
    finally:
        b.close()


def c_existing_file_is_backed_up_and_other_keys_kept():
    b = Box()
    try:
        b.write({"mcpServers": {"old": {"command": PY, "args": []}},
                 "toolGuardian": {"groups": {"code": ["old"]}}})
        code, _ = b.run("add", "new", "--", PY)
        d = b.read()
        return code == 0 and len(b.backups()) >= 1 and set(d["mcpServers"]) == {"old", "new"} \
            and d.get("toolGuardian") == {"groups": {"code": ["old"]}}
    finally:
        b.close()


def c_duplicate_refused_without_replace():
    b = Box()
    try:
        b.write({"mcpServers": {"git": {"command": PY, "args": ["a"]}}})
        before = open(b.cfg, encoding="utf-8").read()
        code, out = b.run("add", "git", "--", PY, "b")
        after = open(b.cfg, encoding="utf-8").read()
        return code != 0 and before == after and "--replace" in out
    finally:
        b.close()


def c_replace_overwrites():
    b = Box()
    try:
        b.write({"mcpServers": {"git": {"command": PY, "args": ["a"]}}})
        code, _ = b.run("add", "git", "--replace", "--", PY, "b")
        return code == 0 and b.read()["mcpServers"]["git"]["args"] == ["b"] and len(b.backups()) >= 1
    finally:
        b.close()


def c_missing_command_is_reported_with_fix():
    b = Box()
    try:
        code, out = b.run("add", "ghost", "--", NOPE, "x")
        return "not found on PATH" in out and "fix:" in out and "ghost" in b.read()["mcpServers"]
    finally:
        b.close()


def c_unset_var_in_args_or_env_is_warned():
    b = Box()
    try:
        b.env["PROBE_SET_VAR"] = "yes"
        code, out = b.run("add", "tok", "--env", "TOKEN=${PROBE_UNSET_VAR}", "--", PY, "--x", "${PROBE_SET_VAR}")
        warn = [ln for ln in out.splitlines() if "not set" in ln]
        return code == 0 and any("PROBE_UNSET_VAR" in ln for ln in warn) and not any("PROBE_SET_VAR" in ln for ln in warn)
    finally:
        b.close()


def c_var_set_in_dotenv_is_not_warned():
    b = Box()
    try:
        os.makedirs(os.path.dirname(b.cfg), exist_ok=True)
        with open(os.path.join(os.path.dirname(b.cfg), ".env"), "w", encoding="utf-8") as fh:
            fh.write("PROBE_UNSET_VAR=fromdotenv\n")
        code, out = b.run("add", "tok", "--env", "TOKEN=${PROBE_UNSET_VAR}", "--", PY)
        return code == 0 and not any("PROBE_UNSET_VAR" in ln and "not set" in ln for ln in out.splitlines())
    finally:
        b.close()


def c_ends_with_doctor_for_that_server_and_restart_line():
    b = Box()
    try:
        b.write({"mcpServers": {"other": {"command": NOPE, "args": []}}})
        code, out = b.run("add", "git", "--", PY)
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        return code == 0 and lines[-1].strip() == RESTART and NOPE not in out and "git" in out
    finally:
        b.close()


def c_no_config_anywhere_writes_home_default():
    b = Box(use_env=False)
    try:
        code, _ = b.run("add", "git", "--", PY)
        p = os.path.join(b.dir, ".tool-guardian", "mcp.json")
        return code == 0 and os.path.isfile(p) and "git" in b.read(p)["mcpServers"]
    finally:
        b.close()


def c_explicit_config_wins():
    b = Box()
    try:
        other = os.path.join(b.dir, "explicit.json")
        code, _ = b.run("add", "git", "--config", other, "--", PY)
        return code == 0 and "git" in b.read(other)["mcpServers"] and not os.path.exists(b.cfg)
    finally:
        b.close()


def c_corrupt_config_is_refused_not_overwritten():
    b = Box()
    try:
        os.makedirs(os.path.dirname(b.cfg), exist_ok=True)
        with open(b.cfg, "w", encoding="utf-8") as fh:
            fh.write('{"mcpServers": {"keep": {"command": "x"},}}')   # trailing comma: a typo, not an empty file
        before = open(b.cfg, encoding="utf-8").read()
        c1, o1 = b.run("add", "git", "--", PY)
        c2, o2 = b.run("remove", "keep")
        after = open(b.cfg, encoding="utf-8").read()
        return c1 != 0 and c2 != 0 and before == after and b.backups() == [] and "JSON" in o1
    finally:
        b.close()


def c_remove_backs_up_and_keeps_the_rest():
    b = Box()
    try:
        b.write({"mcpServers": {"a": {"command": PY}, "b": {"command": PY}}, "toolGuardian": {"x": 1}})
        code, _ = b.run("remove", "a")
        d = b.read()
        return code == 0 and set(d["mcpServers"]) == {"b"} and d.get("toolGuardian") == {"x": 1} and len(b.backups()) >= 1
    finally:
        b.close()


def c_remove_unknown_fails_and_writes_nothing():
    b = Box()
    try:
        b.write({"mcpServers": {"a": {"command": PY}}})
        code, out = b.run("remove", "zzz")
        return code != 0 and b.backups() == [] and set(b.read()["mcpServers"]) == {"a"} and "zzz" in out
    finally:
        b.close()


def c_list_default_group_is_server_name():
    b = Box()
    try:
        b.write({"mcpServers": {"git": {"command": PY, "args": []}, "fs": {"command": "npx", "args": []}}})
        code, out = b.run("list")
        rows = {ln.split()[0]: ln.split() for ln in out.splitlines() if ln.split() and ln.split()[0] in ("git", "fs")}
        return code == 0 and rows["git"][1] == PY and rows["git"][-1] == "git" and rows["fs"][-1] == "fs"
    finally:
        b.close()


def c_list_custom_groups_put_uncovered_in_other():
    b = Box()
    try:
        b.write({"mcpServers": {"git": {"command": PY}, "fs": {"command": PY}},
                 "toolGuardian": {"groups": {"code": ["git"]}}})
        code, out = b.run("list")
        rows = {ln.split()[0]: ln.split() for ln in out.splitlines() if ln.split() and ln.split()[0] in ("git", "fs")}
        return code == 0 and rows["git"][-1] == "code" and rows["fs"][-1] == "other"
    finally:
        b.close()


def c_help_names_the_new_commands():
    b = Box()
    try:
        code, out = b.run("--help")
        return code == 0 and all(w in out for w in ("add", "remove", "list", "import", "doctor"))
    finally:
        b.close()


def c_selftest_still_passes():
    b = Box()
    try:
        code, out = b.run("--selftest")
        return code == 0
    finally:
        b.close()


CHECKS = [(n[2:], f) for n, f in sorted(globals().items()) if n.startswith("c_") and callable(f)]

if __name__ == "__main__":
    passed = 0
    for name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001
            ok = False
            name += " (%s: %s)" % (type(exc).__name__, exc)
        passed += ok
        print("  %s %s" % ("ok  " if ok else "FAIL", name))
    print("probe_tg_setup_add: %d checks, %d passed, %d failed" % (len(CHECKS), passed, len(CHECKS) - passed))
    sys.exit(0 if passed == len(CHECKS) else 1)
