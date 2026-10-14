"""The "Later" list from the 2026-10-10 review triage, one section per item (2026-10-14).
Each test here failed on main before its change."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tool_guardian as tg  # noqa: E402


# ---- 1. nested validate_args -----------------------------------------------------

NESTED = {"type": "object", "required": ["params"], "properties": {
    "params": {"type": "object", "required": ["filters"], "properties": {
        "limit": {"type": "integer"},
        "filters": {"type": "array", "items": {
            "type": "object", "required": ["field", "op"], "additionalProperties": False,
            "properties": {"field": {"type": "string"},
                           "op": {"type": "string", "enum": ["eq", "lt", "gt"]},
                           "value": {"type": "number"}}}}}}}}


def _filters(*extra):
    return [{"field": "a", "op": "eq", "value": 1}, {"field": "b", "op": "lt", "value": 2}, *extra]


def test_nested_valid_call_passes_and_coerces_safely():
    ok, fixed, problems = tg.validate_args(NESTED, {"params": {"limit": "5", "filters": _filters(
        {"field": "c", "op": "gt", "value": "3.5"})}})
    assert (ok, problems) == (True, [])
    assert fixed["params"]["limit"] == 5 and fixed["params"]["filters"][2]["value"] == 3.5


def test_nested_enum_fault_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters(
        {"field": "c", "op": "like", "value": 1})}})
    assert not ok and problems == ["argument 'params.filters[2].op' must be one of: eq, lt, gt"]


def test_nested_missing_required_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters({"field": "c"})}})
    assert not ok and problems == ["missing required argument 'params.filters[2].op' (string)"]


def test_nested_type_fault_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": _filters(
        {"field": "c", "op": "eq", "value": "lots"})}})
    assert not ok and problems == ["argument 'params.filters[2].value' should be number, got str"]


def test_nested_closed_object_names_the_full_path():
    ok, _f, problems = tg.validate_args(NESTED, {"params": {"filters": [
        {"field": "a", "op": "eq", "colour": "red"}]}})
    assert not ok and problems == ["unknown argument 'params.filters[0].colour'; allowed: field, op, value"]
