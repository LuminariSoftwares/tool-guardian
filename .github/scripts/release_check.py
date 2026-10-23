"""Decide what a release publishes. Used by .github/workflows/release.yml (job `verify`).

PyPI and npm are independent version lines. The release tag names the PyPI version: it must be
`v` + pyproject.toml's version exactly as written there (PEP 440). package.json's version is
published to npm only when that exact version is not already on the registry. A release that has
nothing new for either registry is an error; on a dry run it is only a warning.

Writes version, npm_version, prerelease, pypi and npm (true/false) to $GITHUB_OUTPUT.
Standard library only, Python 3.9+.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request


def read_pyproject(path):
    """(name, version) from the [project] table, without a TOML parser (3.9 has none)."""
    table, found = None, {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            head = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
            if head:
                table = head.group(1).strip()
                continue
            kv = re.match(r'^\s*(name|version)\s*=\s*"([^"]+)"', line)
            if table == "project" and kv:
                found[kv.group(1)] = kv.group(2)
    return found["name"], found["version"]


def read_package(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["name"], data["version"]


def pypi_has(name, version):
    url = "https://pypi.org/pypi/%s/%s/json" % (name, version)
    try:
        with urllib.request.urlopen(url, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def npm_has(name, version):
    """True when `npm view <name>@<version> version` prints that version."""
    run = subprocess.run(["npm", "view", "%s@%s" % (name, version), "version"],
                         capture_output=True, text=True, shell=os.name == "nt")
    if run.returncode == 0:
        return run.stdout.strip() != ""
    if "E404" in run.stderr:
        return False   # npm 10/11 exit 1 with E404 for a version (or package) it does not have
    raise RuntimeError("npm view failed: %s" % run.stderr.strip()[-500:])


def plan(tag, version, npm_version, publish, pypi_has, npm_has):
    """What to publish. pypi_has / npm_has take a version and say whether it is on the registry."""
    out = {"errors": [], "warnings": [], "notices": [], "pypi": False, "npm": False,
           "prerelease": "-" in npm_version}
    if tag != "v" + version:
        out["errors"].append("tag %s != v + pyproject.toml version %s" % (tag, version))
        return out
    out["pypi"] = not pypi_has(version)
    out["npm"] = not npm_has(npm_version)
    if not out["pypi"]:
        out["notices"].append("PyPI %s is already on PyPI; PyPI publish skipped" % version)
    if not out["npm"]:
        out["notices"].append("npm %s is already on npm; npm publish skipped" % npm_version)
    if not (out["pypi"] or out["npm"]):
        msg = "nothing to publish: PyPI %s and npm %s are both already released" % (version, npm_version)
        (out["errors"] if publish else out["warnings"]).append(msg)
    if not publish:
        out["pypi"] = out["npm"] = False
    return out


def main():
    env = os.environ
    name, version = read_pyproject("pyproject.toml")
    npm_name, npm_version = read_package("package.json")
    event, ref = env["EVENT"], env["REF"]
    if event == "release":
        tag, publish = env["RELEASE_TAG"], True
    elif event == "workflow_dispatch" and ref.startswith("refs/tags/"):
        tag, publish = env["REF_NAME"], env.get("DRY_RUN") == "false"
    else:
        tag, publish = "v" + version, False
        print("no release tag on %s; checking against v%s" % (event, version))
    print("tag=%s pyproject=%s package.json=%s publish=%s" % (tag, version, npm_version, publish))
    p = plan(tag, version, npm_version, publish,
             pypi_has=lambda v: pypi_has(name, v), npm_has=lambda v: npm_has(npm_name, v))
    for kind in ("notices", "warnings", "errors"):
        for msg in p[kind]:
            print("::%s::%s" % (kind[:-1], msg))
    print("plan: pypi=%s npm=%s prerelease=%s" % (p["pypi"], p["npm"], p["prerelease"]))
    if p["errors"]:
        return 1
    with open(env["GITHUB_OUTPUT"], "a") as fh:
        for key, value in (("version", version), ("npm_version", npm_version),
                           ("prerelease", p["prerelease"]), ("pypi", p["pypi"]), ("npm", p["npm"])):
            fh.write("%s=%s\n" % (key, str(value).lower() if isinstance(value, bool) else value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
