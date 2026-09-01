"""
tg_env.py - environment file loader and variable expansion for MCP router.

Standalone, standard-library-only Python 3.11 module providing two functions
plus a --selftest CLI:

    load_env_file(path=None, override=False, environ=None) -> int
    expand_args(args, environ=None) -> list
"""

import argparse
import os
import re
import sys
import tempfile


# ---------------------------------------------------------------------------
# FUNCTION 1: load_env_file
# ---------------------------------------------------------------------------

def _resolve_env_path(path, environ):
    """Determine which .env file to load based on path / environ.

    Resolution order: explicit path -> TOOL_GUARDIAN_ENV -> an upward search
    for a '.env' starting at the config file's directory (TOOL_GUARDIAN_CONFIG)
    or the cwd, walking up to 5 parent directories. The upward walk lets the
    config live in a nested dir (e.g. config/dsh/) while the .env sits at the
    project root. Falls back to '<start>/.env' (may not exist -> load returns 0).
    """
    if path is not None:
        return path
    if environ.get('TOOL_GUARDIAN_ENV'):
        return environ['TOOL_GUARDIAN_ENV']
    cfg = environ.get('TOOL_GUARDIAN_CONFIG')
    start = os.path.dirname(os.path.abspath(cfg)) if cfg else os.getcwd()
    d = start
    for _ in range(6):
        cand = os.path.join(d, '.env')
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.join(start, '.env')


def _split_unquoted_value(value):
    """
    For an UNQUOTED value string, strip a trailing ' #...' comment if present.
    Otherwise return the value unchanged.

    We use a small parser rather than a single find() so we can be precise
    about when ' #' qualifies as a comment marker.
    """
    # Walk through the string; whenever we see ' #' (whitespace+#) that is
    # NOT inside quotes (we're in the unquoted branch so there are no
    # quotes), stop there. To be safe we only treat ' #' as a comment when
    # it is preceded by a space, a tab, or the start of the string.
    i = 0
    n = len(value)
    while i < n:
        if value[i] == '#':
            # Boundary check: is this '#' preceded by whitespace (or start)?
            if i == 0 or value[i - 1] in (' ', '\t'):
                return value[:i].rstrip()
        i += 1
    return value


def _parse_env_line(raw_line):
    """
    Parse a single .env line.

    Returns (key, value) on success, or None if the line should be skipped
    (comment, blank, malformed).
    """
    line = raw_line.rstrip('\n').rstrip('\r')
    stripped_left = line.lstrip()

    # Full-line comments / blank lines
    if not stripped_left or stripped_left[0] == '#':
        return None

    # Optional 'export ' prefix
    if stripped_left.startswith('export '):
        stripped_left = stripped_left[len('export '):].lstrip()

    if '=' not in stripped_left:
        return None

    key, value = stripped_left.split('=', 1)
    key = key.strip()
    if not key:
        return None

    # Decide how to interpret the value based on whether it starts with a
    # quote character.
    value = value.lstrip()  # consume leading whitespace around ' = '
    if value and value[0] in ('"', "'"):
        quote = value[0]
        # Find the matching closing quote. If present, the value ends there.
        # Anything after the closing quote is then subject to an unquoted
        # trailing-comment strip.
        end = value.find(quote, 1)
        if end != -1:
            inner = value[1:end]
            tail = value[end + 1:]
        else:
            # Unmatched leading quote: take everything after the quote as the
            # raw value, then strip a trailing comment.
            inner = value[1:]
            tail = ''
        # Strip trailing comment from the tail (unquoted territory).
        tail = _split_unquoted_value(tail)
        # If the tail has non-whitespace content, treat it as an inline
        # continuation; but the .env spec doesn't really support that, so
        # we discard it for safety. Most loaders do the same.
        if tail.strip():
            # Inline content after closing quote is non-standard; ignore it.
            pass
        return key, inner

    # Unquoted value: strip a trailing ' #...' comment.
    value = _split_unquoted_value(value)
    value = value.strip()
    return key, value


def load_env_file(path=None, override=False, environ=None):
    """
    Load KEY=VALUE pairs from a .env file into environ.

    Returns the number of keys actually set.
    """
    if environ is None:
        environ = os.environ

    resolved = _resolve_env_path(path, environ)

    # Spec: "If the resolved file does not exist, return 0 (never raise)."
    if not os.path.isfile(resolved):
        return 0

    count = 0
    with open(resolved, 'r', encoding='utf-8') as fh:
        for raw in fh:
            parsed = _parse_env_line(raw)
            if parsed is None:
                continue
            key, value = parsed
            if override:
                environ[key] = value
                count += 1
            else:
                if key not in environ:
                    environ[key] = value
                    count += 1
                # else: ambient/real env wins, do not replace
    return count


# ---------------------------------------------------------------------------
# FUNCTION 2: expand_args
# ---------------------------------------------------------------------------

# Matches ${NAME}, $NAME, %NAME% where NAME is [A-Za-z_][A-Za-z0-9_]*
_VAR_PATTERN = re.compile(
    r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}'
    r'|\$([A-Za-z_][A-Za-z0-9_]*)'
    r'|%([A-Za-z_][A-Za-z0-9_]*)%'
)


def _expand_one(s, environ):
    """Expand ${NAME}, $NAME, %NAME% in s using environ. Unknown left literal."""

    def repl(match):
        name = match.group(1) or match.group(2) or match.group(3)
        if name in environ:
            return environ[name]
        return match.group(0)

    return _VAR_PATTERN.sub(repl, s)


def expand_args(args, environ=None):
    """
    Return a new list where each str item has variable references expanded
    against environ. Non-str items pass through unchanged.
    """
    if environ is None:
        environ = os.environ
    out = []
    for item in args:
        if isinstance(item, str):
            out.append(_expand_one(item, environ))
        else:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _selftest():
    saved = dict(os.environ)
    try:
        # ---- (a) parse KEY=val, '# comment', 'export KEY2="quoted val"',
        #           malformed line
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.env', delete=False, encoding='utf-8'
        ) as f:
            f.write('KEY=val\n')
            f.write('# this is a comment\n')
            f.write('export KEY2="quoted val"\n')
            f.write('MALFORMED LINE WITH NO EQUALS\n')
            tmp_path = f.name

        try:
            env_a = {}
            n = load_env_file(tmp_path, environ=env_a)
            assert n == 2, f"expected 2 keys set, got {n}"
            assert env_a.get('KEY') == 'val', f"KEY wrong: {env_a.get('KEY')!r}"
            assert env_a.get('KEY2') == 'quoted val', (
                f"KEY2 wrong (quotes not stripped?): {env_a.get('KEY2')!r}"
            )
            assert 'MALFORMED LINE WITH NO EQUALS' not in env_a, (
                "malformed line leaked into env"
            )
        finally:
            os.unlink(tmp_path)

        # ---- (b) override=False preserves existing; override=True replaces
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.env', delete=False, encoding='utf-8'
        ) as f:
            f.write('EXISTING=fromfile\n')
            f.write('NEWKEY=newval\n')
            tmp_path = f.name
        try:
            env_b = {'EXISTING': 'fromenv'}
            n = load_env_file(tmp_path, override=False, environ=env_b)
            assert n == 1, f"override=False: expected 1 new key, got {n}"
            assert env_b['EXISTING'] == 'fromenv', (
                f"override=False should preserve ambient: {env_b['EXISTING']!r}"
            )
            assert env_b['NEWKEY'] == 'newval', (
                f"override=False: NEWKEY missing: {env_b.get('NEWKEY')!r}"
            )

            env_b2 = {'EXISTING': 'fromenv'}
            n2 = load_env_file(tmp_path, override=True, environ=env_b2)
            assert n2 == 2, f"override=True: expected 2 keys, got {n2}"
            assert env_b2['EXISTING'] == 'fromfile', (
                f"override=True should replace: {env_b2['EXISTING']!r}"
            )
        finally:
            os.unlink(tmp_path)

        # ---- (c) non-existent path returns 0, never raises
        n = load_env_file('/nonexistent/path/to/file.env', environ={})
        assert n == 0, f"nonexistent path: expected 0, got {n}"

        # ---- (d) expand_args with injected environ
        env_d = {'TOK': 'abc'}
        out = expand_args(['${TOK}', '$TOK', '%TOK%', '${NOPE}', 123], env_d)
        assert out == ['abc', 'abc', 'abc', '${NOPE}', 123], (
            f"expand_args mismatch: {out!r}"
        )

        # ---- (e) upward search: .env at an ancestor of the config dir
        import shutil as _shutil
        top = tempfile.mkdtemp()
        try:
            nested = os.path.join(top, 'config', 'dsh')
            os.makedirs(nested, exist_ok=True)
            with open(os.path.join(top, '.env'), 'w', encoding='utf-8') as ef:
                ef.write('DEEPKEY=deepval\n')
            env_e = {'TOOL_GUARDIAN_CONFIG': os.path.join(nested, 'cfg.json')}
            found = _resolve_env_path(None, env_e)
            assert os.path.isfile(found) and os.path.samefile(
                found, os.path.join(top, '.env')), f"upward search failed: {found!r}"
            env_e2 = {}
            load_env_file(found, environ=env_e2)
            assert env_e2.get('DEEPKEY') == 'deepval', "upward .env not loaded"
        finally:
            _shutil.rmtree(top, ignore_errors=True)

        print('SELFTEST OK')
        return 0
    finally:
        for k in list(os.environ.keys()):
            if k not in saved:
                del os.environ[k]
        for k, v in saved.items():
            if os.environ.get(k) != v:
                os.environ[k] = v


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='tg_env - environment loader & variable expansion'
    )
    parser.add_argument(
        '--selftest', action='store_true',
        help='run the built-in self test and exit'
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
