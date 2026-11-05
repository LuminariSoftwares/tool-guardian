"""tg_intent: return only the part of a big tool result that matches a stated intent.

The idea (hand the model just the slice of a large result that matches what it says it
wants, plus one line of searchable terms) comes from context-mode (mksglu/context-mode,
Elastic License 2.0). This module is rewritten from the idea; no code, tests, prose or
identifiers were copied from that project.

Pure functions, standard library only, no files, no network:
    chunk(text)                         -> list[str]
    score(chunk_text, intent)           -> int
    filter_text(text, intent, max_bytes) -> (kept, terms)

Run `python tg_intent.py --selftest` to self-check.
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter
from typing import List, Tuple

MAX_LINES = 40
JOINER = "\n...\n"
TERM_COUNT = 12
_HEADING = re.compile(r"^#{1,6}\s")
_TOKEN = re.compile(r"[a-z0-9_]+")
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have he her his i if in into is it its of on or our "
    "she so than that the their them then there these they this to was we were what when which who "
    "will with you your not no do does did can could should would may might been being also".split()
)


def _tokens(text: str) -> List[str]:
    return _TOKEN.findall(text.lower())


def chunk(text: str) -> List[str]:
    """Split on headings and blank lines, then into pieces of at most 40 lines."""
    out: List[str] = []
    cur: List[str] = []

    def flush() -> None:
        for i in range(0, len(cur), MAX_LINES):
            out.append("\n".join(cur[i:i + MAX_LINES]))
        del cur[:]

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        if _HEADING.match(line):
            flush()
        cur.append(line)
    flush()
    return out


def score(chunk_text: str, intent: str) -> int:
    """Distinct intent words present as whole tokens; doubled on a contiguous phrase match."""
    words = _tokens(intent)
    if not words:
        return 0
    toks = _tokens(chunk_text)
    present = set(toks)
    total = len(set(words) & present)
    if total and len(words) >= 2:
        n = len(words)
        if any(toks[i:i + n] == words for i in range(len(toks) - n + 1)):
            total *= 2
    return total


def _terms(text: str) -> str:
    counts = Counter(t for t in _tokens(text) if t not in _STOPWORDS and not t.isdigit())
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TERM_COUNT]
    return "searchable terms: " + ", ".join(t for t, _ in top)


def filter_text(text: str, intent: str, max_bytes: int) -> Tuple[str, str]:
    """Keep the best-matching chunks (original order, within max_bytes) and a terms line."""
    chunks = chunk(text)
    total = len(chunks)
    ranked = sorted(
        ((score(c, intent), i) for i, c in enumerate(chunks)), key=lambda p: (-p[0], p[1])
    )
    chosen: List[int] = []
    kept = ""
    for sc, idx in ranked:
        if sc <= 0:
            break
        trial = sorted(chosen + [idx])
        body = JOINER.join("[chunk %d/%d] %s" % (i + 1, total, chunks[i]) for i in trial)
        if len(body.encode("utf-8")) > max_bytes:
            break
        chosen, kept = trial, body
    return kept, _terms(text)


def _digest(pair: Tuple[str, str]) -> str:
    return hashlib.sha256("\x00".join(pair).encode("utf-8")).hexdigest()


def _sample() -> str:
    return "\n\n".join(
        "Paragraph %d about filler %d.%s" % (i, i, " The csv exporter writes rows." if i == 3 else "")
        for i in range(8)
    )


def _t_headings() -> bool:
    ch = chunk("# A\nx\n## B\ny\n\nz\n# C\nw")
    return ch == ["# A\nx", "## B\ny", "z", "# C\nw"]


def _t_blank_dropped() -> bool:
    return chunk("a\n\n\n\nb") == ["a", "b"] and chunk("") == [] and chunk("\n \n") == []


def _t_40_split() -> bool:
    text = "\n".join("r%d" % i for i in range(100))
    ch = chunk(text)
    return [len(c.splitlines()) for c in ch] == [40, 40, 20] and "\n".join(ch) == text


def _t_order() -> bool:
    text = "# H\na\nb\n\nc\n## I\nd"
    flat = [ln for c in chunk(text) for ln in c.splitlines()]
    return flat == [ln for ln in text.splitlines() if ln.strip()]


def _t_score_examples() -> bool:
    return (score("The CSV exporter writes files.", "csv exporter") == 4
            and score("The exporter writes files as csv.", "csv exporter") == 2
            and score("Exporter!", "exporter") == 1)


def _t_score_whole_token() -> bool:
    return score("many exporters", "exporter") == 0 and score("x", "") == 0


def _t_score_single_no_double() -> bool:
    return score("csv csv csv", "csv") == 1


def _t_filter_prefix() -> bool:
    kept, _ = filter_text(_sample(), "csv exporter", 1000)
    return kept.startswith("[chunk 4/8] ") and "Paragraph 3" in kept and "Paragraph 5" not in kept


def _t_filter_bound() -> bool:
    kept, _ = filter_text(_sample(), "paragraph", 120)
    return 0 < len(kept.encode("utf-8")) <= 120 and filter_text(_sample(), "paragraph", 0)[0] == ""


def _t_filter_order_tie() -> bool:
    kept, _ = filter_text(_sample(), "paragraph", 10000)
    pos = [kept.find("Paragraph %d " % i) for i in range(8)]
    return pos == sorted(pos) and min(pos) >= 0 and kept.count("\n...\n") == 7


def _t_no_match_empty() -> bool:
    return filter_text(_sample(), "zebra", 1000)[0] == ""


def _t_terms() -> bool:
    _, terms = filter_text("the the the 123 123 apple apple pear zed\n\nbeta", "x", 10)
    return terms == "searchable terms: apple, beta, pear, zed"


def _t_deterministic() -> bool:
    return _digest(filter_text(_sample(), "csv exporter", 500)) == _digest(filter_text(_sample(), "csv exporter", 500))


CHECKS = [
    ("chunk_headings", _t_headings), ("chunk_blank_dropped", _t_blank_dropped),
    ("chunk_40_lines", _t_40_split), ("chunk_order_preserved", _t_order),
    ("score_examples", _t_score_examples), ("score_whole_token", _t_score_whole_token),
    ("score_single_word", _t_score_single_no_double), ("filter_prefix", _t_filter_prefix),
    ("filter_byte_bound", _t_filter_bound), ("filter_original_order", _t_filter_order_tie),
    ("filter_no_match", _t_no_match_empty), ("terms_line", _t_terms),
    ("deterministic", _t_deterministic),
]


def _selftest() -> int:
    results = []
    for name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception:  # noqa: BLE001
            ok = False
        results.append((name, ok))
        if not ok:
            print("FAIL " + name)
    n = len(results)
    p = sum(1 for _, ok in results if ok)
    print("tg_intent selftest: %d checks, %d passed, %d failed" % (n, p, n - p))
    return 0 if n > 0 and p == n else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    print(__doc__)
