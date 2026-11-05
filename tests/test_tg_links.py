"""tg_links: the in-file selftest, the contract examples, and the bench's one-line output."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tg_links  # noqa: E402

CAT = {"gh": [
    {"name": "list_issues", "description": "List issues.", "outputSchema": {"properties": {"issueId": {}}}},
    {"name": "get_issue", "inputSchema": {"required": ["issue_id"]}},
    {"name": "find_repo", "description": "Returns the repo_name of each match."},
    {"name": "clone_repo", "inputSchema": {"required": ["repo_name"]}},
    {"name": "echo", "description": "Echo; returns the text.", "inputSchema": {"required": ["text"]}}]}


def test_selftest_count_line():
    r = subprocess.run([sys.executable, str(ROOT / "tg_links.py"), "--selftest"],
                       capture_output=True, text=True, timeout=60)
    m = re.search(r"tg_links selftest: (\d+) checks, (\d+) passed, (\d+) failed", r.stdout)
    assert r.returncode == 0 and m and int(m.group(1)) >= 6 and m.group(3) == "0", r.stdout + r.stderr


def test_output_schema_and_description_links():
    got = tg_links.link(CAT)
    assert got[("gh", "get_issue")] == [("issue_id", "gh.list_issues")]
    assert got[("gh", "clone_repo")] == [("repo_name", "gh.find_repo")]


def test_no_self_link_and_only_linked_keys():
    got = tg_links.link(CAT)
    assert ("gh", "echo") not in got
    assert all(prod != "%s.%s" % k for k, v in got.items() for _p, prod in v)


def test_bench_prints_one_line():
    r = subprocess.run([sys.executable, str(ROOT / "bench" / "bench_links.py")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(r"producer recall: baseline \d+(\.\d+)?% links \d+(\.\d+)?%", r.stdout.strip()), r.stdout
