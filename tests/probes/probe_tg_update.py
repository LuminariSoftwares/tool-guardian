# @studio: QA | Overseer probe for tool-guardian's tg_update.py -- written from the CONTRACT, not from the module
# @kind: cli
# @called_by: human | tests/test_contract_probes.py
"""py probe_tg_update.py [dir-holding-tg_update.py]   -> last line: probe_tg_update: N checks, N passed, M failed"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "."))
import tg_update as U  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="probe_tg_update_"))
NOW = 1790000000.0          # seconds
DAY = 86400


def _http(payloads, calls):
    """A fake http_get: url -> bytes, recording every URL asked for."""
    def get(url, timeout):
        calls.append((url, timeout))
        body = payloads.get(url)
        if body is None:
            raise OSError("offline")
        return body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return get


NPM_URL = "https://registry.npmjs.org/-/package/dsh-tool-guardian/dist-tags"
PYPI_URL = "https://pypi.org/pypi/tool-guardian/json"


def _npm_info():
    return {"kind": "npm", "name": "dsh-tool-guardian", "current": "0.3.0-alpha.4",
            "update_cmd": "dsh plugin --profile <name> update dsh-tool-guardian"}


def _pypi_info(current="0.3.0"):
    return {"kind": "pypi", "name": "tool-guardian", "current": current,
            "update_cmd": "pip install -U tool-guardian"}


def c_opt_out_semantics_match_the_node_notice():
    return (U.opted_out({"GUARDIAN_NO_UPDATE_CHECK": "1"}) and U.opted_out({"CI": "true"})
            and U.opted_out({"NO_UPDATE_NOTIFIER": "yes"}) and not U.opted_out({})
            and not U.opted_out({"CI": "0"}) and not U.opted_out({"CI": " FALSE "})
            and not U.opted_out({"GUARDIAN_NO_UPDATE_CHECK": ""}))


def c_compare_semver_and_pep440():
    c = U.compare_versions
    return (c("0.3.0", "0.3.0-alpha.4") == 1 and c("0.3.0-alpha.4", "0.3.0-alpha.10") == -1
            and c("0.3.0-alpha.4", "0.3.0-beta.1") == -1 and c("1.0.0", "0.9.9") == 1
            and c("0.3.0a4", "0.3.0-alpha.4") == 0 and c("0.3.0rc1", "0.3.0") == -1
            and c("0.3.0b2", "0.3.0a9") == 1 and c("junk", "0.1.0") == 0 and c("0.3.0", "0.3.0") == 0)


def c_pick_update_respects_channel():
    tags = ["0.3.0-alpha.4", "0.3.0-alpha.5", "0.2.0", "garbage"]
    return (U.pick_update("0.3.0-alpha.4", tags) == "0.3.0-alpha.5"
            and U.pick_update("0.2.0", tags) is None
            and U.pick_update("0.2.0", tags + ["0.3.0"]) == "0.3.0"
            and U.pick_update("0.3.0-alpha.5", tags) is None
            and U.pick_update("0.3.0", ["0.1.0"]) is None)


def c_install_kind_npm_vs_pypi():
    npm_root = TMP / "npmpkg"
    npm_root.mkdir()
    (npm_root / "package.json").write_text(json.dumps({"name": "dsh-tool-guardian", "version": "0.3.0-alpha.5"}),
                                           encoding="utf-8")
    other = TMP / "otherpkg"
    other.mkdir()
    (other / "package.json").write_text(json.dumps({"name": "something-else", "version": "9.9.9"}), encoding="utf-8")
    bare = TMP / "bare"
    bare.mkdir()
    npm = U.install_kind(npm_root, py_version="0.3.0")
    py1 = U.install_kind(other, py_version="0.3.0")
    py2 = U.install_kind(bare, py_version="0.3.0")
    return (npm["kind"] == "npm" and npm["name"] == "dsh-tool-guardian" and npm["current"] == "0.3.0-alpha.5"
            and "dsh plugin" in npm["update_cmd"] and "update dsh-tool-guardian" in npm["update_cmd"]
            and py1 == py2 and py1["kind"] == "pypi" and py1["name"] == "tool-guardian"
            and py1["current"] == "0.3.0" and "pip install -U tool-guardian" in py1["update_cmd"])


def c_default_package_root_is_the_module_folder():
    info = U.install_kind()          # no overrides: resolves beside tg_update.py
    here = Path(U.__file__).resolve().parent
    pkg = here / "package.json"
    want = "npm" if pkg.is_file() and json.loads(pkg.read_text(encoding="utf-8")).get("name") == "dsh-tool-guardian" else "pypi"
    return info["kind"] == want and isinstance(info["current"], str) and bool(info["current"])


def c_cache_file_npm_is_shared_with_the_node_notice():
    home = TMP / "home_cf"
    return (U.cache_file("npm", "dsh-tool-guardian", home=home)
            == home / ".cache" / "dsh-guardians" / "dsh-tool-guardian.update.json"
            and U.cache_file("pypi", "tool-guardian", home=home)
            == home / ".cache" / "tool-guardian" / "tool-guardian.update.json")


def c_fetch_candidates_npm_and_pypi_and_failure():
    calls = []
    get = _http({NPM_URL: {"latest": "0.3.0-alpha.4", "alpha": "0.3.0-alpha.5"},
                 PYPI_URL: {"info": {"version": "0.3.1"}, "releases": {"0.1.0": [{}], "0.3.1": [{}], "0.3.2": []}}},
                calls)
    npm = U.fetch_candidates("npm", "dsh-tool-guardian", http_get=get)
    pypi = U.fetch_candidates("pypi", "tool-guardian", http_get=get)
    bad = U.fetch_candidates("npm", "nope", http_get=get)
    return (sorted(npm) == ["0.3.0-alpha.4", "0.3.0-alpha.5"] and sorted(pypi) == ["0.1.0", "0.3.1"]
            and bad is None and [u for u, _ in calls][:2] == [NPM_URL, PYPI_URL] and all(t <= 5 for _, t in calls))


def c_check_update_writes_node_compatible_cache():
    home = TMP / "home_up"
    calls = []
    res = U.check(_npm_info(), env={}, now=lambda: NOW, http_get=_http({NPM_URL: {"alpha": "0.3.0-alpha.5"}}, calls), home=home)
    cache = json.loads(U.cache_file("npm", "dsh-tool-guardian", home=home).read_text(encoding="utf-8"))
    return (res["status"] == "update" and res["next"] == "0.3.0-alpha.5" and res["current"] == "0.3.0-alpha.4"
            and cache == {"checkedAt": int(NOW * 1000), "next": "0.3.0-alpha.5"} and len(calls) == 1)


def c_check_fresh_cache_means_no_network():
    home = TMP / "home_fresh"
    f = U.cache_file("npm", "dsh-tool-guardian", home=home)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"checkedAt": int((NOW - 3600) * 1000), "next": None}), encoding="utf-8")
    calls = []
    res = U.check(_npm_info(), env={}, now=lambda: NOW, http_get=_http({}, calls), home=home)
    return res["status"] == "current" and calls == []


def c_check_stale_cache_refetches():
    home = TMP / "home_stale"
    f = U.cache_file("npm", "dsh-tool-guardian", home=home)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"checkedAt": int((NOW - 2 * DAY) * 1000), "next": None}), encoding="utf-8")
    calls = []
    res = U.check(_npm_info(), env={}, now=lambda: NOW,
                  http_get=_http({NPM_URL: {"latest": "0.3.0"}}, calls), home=home)
    return res["status"] == "update" and res["next"] == "0.3.0" and len(calls) == 1


def c_check_offline_is_unknown_and_writes_nothing():
    home = TMP / "home_off"
    res = U.check(_pypi_info(), env={}, now=lambda: NOW, http_get=_http({}, []), home=home)
    return res["status"] == "unknown" and not U.cache_file("pypi", "tool-guardian", home=home).exists()


def c_check_opted_out_never_touches_network():
    calls = []
    res = U.check(_pypi_info(), env={"GUARDIAN_NO_UPDATE_CHECK": "1"}, now=lambda: NOW,
                  http_get=_http({PYPI_URL: {"releases": {"9.9.9": [{}]}}}, calls), home=TMP / "home_oo")
    return res["status"] == "off" and calls == []


def c_status_lines_are_one_line_and_actionable():
    up = U.status_line({"status": "update", "current": "0.3.0", "next": "0.3.1", "info": _pypi_info()})
    cur = U.status_line({"status": "current", "current": "0.3.0", "next": None, "info": _pypi_info()})
    unk = U.status_line({"status": "unknown", "current": "0.3.0", "next": None, "info": _pypi_info()})
    off = U.status_line({"status": "off", "current": "0.3.0", "next": None, "info": _pypi_info()})
    lines = [up, cur, unk, off]
    return (all(isinstance(x, str) and "\n" not in x and x.startswith("update:") for x in lines)
            and "0.3.1" in up and "pip install -U tool-guardian" in up
            and "0.3.0" in cur and "GUARDIAN_NO_UPDATE_CHECK" in off and "0.3.0" in unk)


def c_check_never_raises_on_garbage():
    home = TMP / "home_bad"
    f = U.cache_file("pypi", "tool-guardian", home=home)
    f.parent.mkdir(parents=True)
    f.write_text("{broken", encoding="utf-8")

    def boom(url, timeout):
        raise ValueError("weird")
    r1 = U.check(_pypi_info(), env={}, now=lambda: NOW, http_get=boom, home=home)
    r2 = U.check(_pypi_info(), env={}, now=lambda: NOW, http_get=_http({PYPI_URL: b"<html>"}, []), home=home)
    return r1["status"] == "unknown" and r2["status"] == "unknown"


CHECKS = [
    ("opt_out_semantics_match_the_node_notice", c_opt_out_semantics_match_the_node_notice),
    ("compare_semver_and_pep440", c_compare_semver_and_pep440),
    ("pick_update_respects_channel", c_pick_update_respects_channel),
    ("install_kind_npm_vs_pypi", c_install_kind_npm_vs_pypi),
    ("default_package_root_is_the_module_folder", c_default_package_root_is_the_module_folder),
    ("cache_file_npm_is_shared_with_the_node_notice", c_cache_file_npm_is_shared_with_the_node_notice),
    ("fetch_candidates_npm_and_pypi_and_failure", c_fetch_candidates_npm_and_pypi_and_failure),
    ("check_update_writes_node_compatible_cache", c_check_update_writes_node_compatible_cache),
    ("check_fresh_cache_means_no_network", c_check_fresh_cache_means_no_network),
    ("check_stale_cache_refetches", c_check_stale_cache_refetches),
    ("check_offline_is_unknown_and_writes_nothing", c_check_offline_is_unknown_and_writes_nothing),
    ("check_opted_out_never_touches_network", c_check_opted_out_never_touches_network),
    ("status_lines_are_one_line_and_actionable", c_status_lines_are_one_line_and_actionable),
    ("check_never_raises_on_garbage", c_check_never_raises_on_garbage),
]


def main():
    passed = 0
    try:
        for name, fn in CHECKS:
            try:
                ok, why = fn() is True, ""
            except Exception as exc:  # noqa: BLE001
                ok, why = False, " (%s: %s)" % (type(exc).__name__, exc)
            print("  %s %s%s" % ("ok  " if ok else "FAIL", name, why))
            passed += 1 if ok else 0
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    total = len(CHECKS)
    print("probe_tg_update: %d checks, %d passed, %d failed" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
