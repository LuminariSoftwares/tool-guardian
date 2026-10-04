"""tg_rank.py -- scoring rule for tool-guardian's Router.search, as a pure helper.

Tool-guardian ranks candidate MCP tools for a user query by scoring each tool
with a fixed additive rule over the query's tokens, with an optional boost for
tokens that appear in a per-server "hint" list (TJ plan P45 B4, C-Hints).

Standard library only. Pure: no network, no filesystem, no process spawning.
"""

import argparse
import re
import sys

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokens_of(query) -> list:
    """Split `query` into lowercase alphanumeric tokens of length > 1.

    Order and duplicates are preserved. None or "" yields [].
    """
    return [t for t in _TOKEN_RE.findall((query or "").lower()) if len(t) > 1]


def score(tool: dict, server: str, tokens: list, hints=None) -> int:
    """Score one tool against the tokens of a query.

    Each token (duplicates included, each counted again) contributes:
      +3 if it is a substring of the lowercased tool name
      +2 if it equals the lowercased server name
      +1 if it is a substring of the lowercased description
      +4 if it is in the lowercased hint set (exact match, not substring)
    """
    tool = tool if isinstance(tool, dict) else {}
    name = str(tool.get("name") or "").lower()
    desc = str(tool.get("description") or "").lower()
    hintset = {str(h).lower() for h in (hints or [])}
    srv = str(server or "").lower()

    total = 0
    for tok in (tokens or []):
        if tok in name:
            total += 3
        if tok == srv:
            total += 2
        if tok in desc:
            total += 1
        if tok in hintset:
            total += 4
    return int(total)


def _check_tokens_basic() -> bool:
    return tokens_of("Hello World") == ["hello", "world"]


def _check_tokens_drops_singletons() -> bool:
    return tokens_of("a bb c dde") == ["bb", "dde"]


def _check_tokens_empty_inputs() -> bool:
    return tokens_of("") == [] and tokens_of(None) == []


def _check_tokens_keeps_duplicates_and_order() -> bool:
    return tokens_of("beta alpha beta") == ["beta", "alpha", "beta"]


def _check_tokens_strips_punctuation() -> bool:
    return tokens_of("read_file(path, 'x-1')") == ["read", "file", "path"]


def _check_score_name_substring() -> bool:
    tool = {"name": "read_file", "description": "loads a file from disk"}
    return score(tool, "", ["read"]) == 3


def _check_score_server_equality_only() -> bool:
    tool = {"name": "noop", "description": ""}
    equal = score(tool, "github", ["github"])
    not_equal = score(tool, "github", ["git"])
    return equal == 2 and not_equal == 0


def _check_score_description_hit() -> bool:
    tool = {"name": "noop", "description": "Searches issues"}
    return score(tool, "", ["issues"]) == 1


def _check_score_hint_exact_not_substring() -> bool:
    tool = {"name": "noop", "description": ""}
    exact = score(tool, "", ["git"], hints=["GIT"])
    partial = score(tool, "", ["git"], hints=["github"])
    return exact == 4 and partial == 0


def _check_score_rules_stack_per_token() -> bool:
    tool = {"name": "github_list_issues", "description": "list issues for a repo"}
    # "github" is in the name (+3) and equals the server (+2), but is absent
    # from the description, so no +1; it is also hinted (+4).
    return score(tool, "github", ["github"], hints=["github"]) == 3 + 2 + 4


def _check_score_duplicates_count_again() -> bool:
    tool = {"name": "git", "description": "git"}
    once = score(tool, "", ["git"])
    twice = score(tool, "", ["git", "git"])
    return once == 4 and twice == 8


def _check_score_missing_keys_and_none() -> bool:
    bare = score({}, None, ["anything"])
    nulled = score({"name": None, "description": None}, None, ["anything"], hints=None)
    return bare == 0 and nulled == 0


def _check_score_no_tokens_is_zero() -> bool:
    tool = {"name": "git", "description": "git"}
    return score(tool, "git", [], hints=["git"]) == 0


def _check_score_returns_int() -> bool:
    tool = {"name": "git", "description": "git"}
    return type(score(tool, "git", ["git"], hints=["git"])) is int


def _check_score_end_to_end_query() -> bool:
    tool = {"name": "issue_list", "description": "List GitHub issues"}
    toks = tokens_of("list git issues")
    # "list": name+3, desc+1, hint+4. "git": desc+1 only.
    # "issues": desc+1 only -- it is not a substring of "issue_list".
    return score(tool, "github", toks, hints=["list"]) == 8 + 1 + 1


def _check_score_name_is_lowercased() -> bool:
    tool = {"name": "GIT_LOG", "description": ""}
    return score(tool, "", ["git", "log"]) == 6


def _check_score_desc_is_lowercased() -> bool:
    tool = {"name": "noop", "description": "Shows GIT Status"}
    return score(tool, "", ["shows", "git", "status"]) == 3


CHECKS = [
    ("tokens_basic", _check_tokens_basic),
    ("tokens_drops_singletons", _check_tokens_drops_singletons),
    ("tokens_empty_inputs", _check_tokens_empty_inputs),
    ("tokens_keeps_duplicates_and_order", _check_tokens_keeps_duplicates_and_order),
    ("tokens_strips_punctuation", _check_tokens_strips_punctuation),
    ("score_name_substring", _check_score_name_substring),
    ("score_server_equality_only", _check_score_server_equality_only),
    ("score_description_hit", _check_score_description_hit),
    ("score_hint_exact_not_substring", _check_score_hint_exact_not_substring),
    ("score_name_is_lowercased", _check_score_name_is_lowercased),
    ("score_desc_is_lowercased", _check_score_desc_is_lowercased),
    ("score_rules_stack_per_token", _check_score_rules_stack_per_token),
    ("score_duplicates_count_again", _check_score_duplicates_count_again),
    ("score_missing_keys_and_none", _check_score_missing_keys_and_none),
    ("score_no_tokens_is_zero", _check_score_no_tokens_is_zero),
    ("score_returns_int", _check_score_returns_int),
    ("score_end_to_end_query", _check_score_end_to_end_query),
]


def run_selftest() -> int:
    """Run every check, print the summary line, return an exit code."""
    passed = 0
    failed = 0
    for name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception as exc:  # a raising check counts as a failure, not a crash
            ok = False
            print("FAIL %s: %s" % (name, exc))
        if ok:
            passed += 1
        else:
            failed += 1
            print("FAIL %s" % (name,))
    total = len(CHECKS)
    print("tg_rank selftest: %d checks, %d passed, %d failed" % (total, passed, failed))
    return 0 if (failed == 0 and total > 0) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg_rank.py", description=__doc__)
    parser.add_argument("--score", metavar="QUERY", help="query to score a tool against")
    parser.add_argument("--name", default="", help="tool name")
    parser.add_argument("--desc", default="", help="tool description")
    parser.add_argument("--server", default="", help="server name")
    parser.add_argument("--hint", action="append", metavar="H", help="server hint token; repeatable")
    parser.add_argument("--selftest", action="store_true", help="run in-file checks and exit")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return run_selftest()
    if args.score is None:
        build_parser().print_usage(sys.stderr)
        print("tg_rank.py: error: --score QUERY is required", file=sys.stderr)
        return 2
    tool = {"name": args.name, "description": args.desc}
    print(score(tool, args.server, tokens_of(args.score), args.hint))
    return 0


if __name__ == "__main__":
    sys.exit(main())