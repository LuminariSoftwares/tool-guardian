"""Tests for tg_intent (intent filter for large tool results)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_intent  # noqa: E402


def _doc():
    return "\n\n".join("Paragraph %d filler.%s" % (i, " The csv exporter." if i == 2 else "") for i in range(6))


def test_chunk_headings_blank_and_order():
    assert tg_intent.chunk("# A\nx\n## B\ny\n\nz") == ["# A\nx", "## B\ny", "z"]


def test_chunk_splits_at_40_lines():
    text = "\n".join("r%d" % i for i in range(85))
    assert [len(c.splitlines()) for c in tg_intent.chunk(text)] == [40, 40, 5]


def test_score_contract_examples():
    s = tg_intent.score
    assert s("The CSV exporter writes files.", "csv exporter") == 4
    assert s("The exporter writes files as csv.", "csv exporter") == 2
    assert s("Exporter!", "exporter") == 1
    assert s("exporters", "exporter") == 0


def test_filter_keeps_match_with_prefix_and_bound():
    kept, terms = tg_intent.filter_text(_doc(), "csv exporter", 500)
    assert kept.startswith("[chunk 3/6] ") and "Paragraph 1" not in kept
    assert terms.startswith("searchable terms: ") and "\n" not in terms
    small, _ = tg_intent.filter_text(_doc(), "paragraph", 80)
    assert 0 < len(small.encode("utf-8")) <= 80


def test_filter_deterministic_and_no_match():
    assert tg_intent.filter_text(_doc(), "csv", 500) == tg_intent.filter_text(_doc(), "csv", 500)
    assert tg_intent.filter_text(_doc(), "zebra", 500)[0] == ""


def test_selftest_passes():
    assert tg_intent._selftest() == 0
