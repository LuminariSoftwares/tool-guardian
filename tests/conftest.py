"""Keep the suite out of the operator's real call log and spill archive
(~/.tool-guardian): on 2026-09-19 a test run wrote a `server: broken` row into the live
calls.jsonl that a real DSH session was being measured from."""
import pytest

import tool_guardian as tg


@pytest.fixture(autouse=True)
def _isolated_guardian_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "CALL_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("TOOL_GUARDIAN_CALL_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("TOOL_GUARDIAN_SPILL_DIR", str(tmp_path / "spill"))
