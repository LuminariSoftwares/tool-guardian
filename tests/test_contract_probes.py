"""Run the overseer-written CONTRACT probes (tests/probes/) against the modules beside
tool_guardian.py. The probes were written from each module's contract before the module
existed, so they assert what the author could not make true by writing a weak selftest."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBES = Path(__file__).resolve().parent / "probes"


@pytest.mark.parametrize("module,minimum", [("tg_ladder", 15), ("tg_spill", 10), ("tg_groups", 7)])
def test_contract_probe(module, minimum):
    if not (ROOT / (module + ".py")).is_file():
        pytest.skip(module + ".py is not in the repo yet")
    r = subprocess.run([sys.executable, str(PROBES / ("probe_%s.py" % module)), str(ROOT)],
                       capture_output=True, text=True, timeout=120)
    m = re.search(r"probe_%s: (\d+) checks, (\d+) passed, (\d+) failed" % module, r.stdout)
    assert m, r.stdout + r.stderr
    total, passed, failed = (int(x) for x in m.groups())
    assert total >= minimum and failed == 0 and passed == total and r.returncode == 0, r.stdout


@pytest.mark.parametrize("module", ["tg_ladder", "tg_spill", "tg_groups", "tg_setup"])
def test_module_selftest_count_line(module):
    if not (ROOT / (module + ".py")).is_file():
        pytest.skip(module + ".py is not in the repo yet")
    r = subprocess.run([sys.executable, str(ROOT / (module + ".py")), "--selftest"],
                       capture_output=True, text=True, timeout=120)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    m = re.fullmatch(r"%s selftest: (\d+) checks, (\d+) passed, (\d+) failed" % module, last.strip())
    assert m, r.stdout[-800:] + r.stderr[-400:]
    assert int(m.group(1)) > 0 and int(m.group(3)) == 0 and r.returncode == 0


def test_setup_add_remove_list_probe():
    """tool-guardian-setup add/remove/list (2026-09-25): the contract probe runs the CLI in a throwaway HOME."""
    r = subprocess.run([sys.executable, str(PROBES / "probe_tg_setup_add.py"), str(ROOT)],
                       capture_output=True, text=True, timeout=300)
    m = re.search(r"probe_tg_setup_add: (\d+) checks, (\d+) passed, (\d+) failed", r.stdout)
    assert m, r.stdout + r.stderr
    total, passed, failed = (int(x) for x in m.groups())
    assert total >= 19 and failed == 0 and passed == total and r.returncode == 0, r.stdout
