"""tool-guardian discovery ceiling (TJ plan P45 B6, C-Ceiling).

A model that keeps listing/searching tools without ever calling one is
stopped after `limit` discovery calls in a row; calling a tool resets
the count.

Standard library only.
"""

import sys

DISCOVERY_TOOLS = (
    "list_capabilities",
    "search_capabilities",
    "describe_tool",
    "list_groups_with_costs",
)

REFUSAL_TEMPLATE = (
    "Discovery limit reached (%d calls without call_tool). "
    "Call a tool now with call_tool, or answer with what you have."
)


class DiscoveryCeiling:
    """Track consecutive discovery calls and refuse once `limit` is reached."""

    def __init__(self, limit: int = 8):
        self.limit = limit
        self.count = 0

    @property
    def refusal(self) -> str:
        """The exact refusal text for this instance's limit."""
        return REFUSAL_TEMPLATE % self.limit

    def note(self, tool_name: str):
        """Call once per tool call, before the call is handled.

        Returns None to let the call through, or the refusal string.
        """
        if tool_name == "call_tool":
            self.count = 0
            return None
        if tool_name in DISCOVERY_TOOLS:
            if self.count >= self.limit:
                return self.refusal
            self.count += 1
            return None
        return None


# --- selftest -------------------------------------------------------------

def check_defaults():
    dc = DiscoveryCeiling()
    return dc.limit == 8 and dc.count == 0


def check_discovery_increments():
    dc = DiscoveryCeiling()
    first = dc.note("list_capabilities")
    second = dc.note("search_capabilities")
    return first is None and second is None and dc.count == 2


def check_all_discovery_tools_count():
    dc = DiscoveryCeiling(limit=10)
    for name in DISCOVERY_TOOLS:
        if dc.note(name) is not None:
            return False
    return dc.count == len(DISCOVERY_TOOLS)


def check_call_tool_resets():
    dc = DiscoveryCeiling(limit=10)
    for name in DISCOVERY_TOOLS:
        dc.note(name)
    before = dc.count
    reset = dc.note("call_tool")
    return before == 4 and reset is None and dc.count == 0


def check_refusal_at_limit():
    dc = DiscoveryCeiling(limit=3)
    for _ in range(3):
        if dc.note("describe_tool") is not None:
            return False
    return dc.note("describe_tool") == dc.refusal


def check_count_pinned_at_limit_on_refusal():
    dc = DiscoveryCeiling(limit=2)
    dc.note("list_capabilities")
    dc.note("list_capabilities")
    for _ in range(5):
        if dc.note("list_capabilities") != dc.refusal:
            return False
    return dc.count == 2


def check_refusal_text_exact():
    dc = DiscoveryCeiling(limit=7)
    expected = (
        "Discovery limit reached (7 calls without call_tool). "
        "Call a tool now with call_tool, or answer with what you have."
    )
    return dc.refusal == expected


def check_other_names_inert():
    dc = DiscoveryCeiling(limit=4)
    dc.note("list_capabilities")
    r1 = dc.note("some_other_tool")
    r2 = dc.note("")
    r3 = dc.note("call_toolXYZ")
    return r1 is None and r2 is None and r3 is None and dc.count == 1


def check_zero_limit_refuses_immediately():
    dc = DiscoveryCeiling(limit=0)
    result = dc.note("list_capabilities")
    return result == dc.refusal and dc.count == 0


def check_call_tool_after_refusal_recovers():
    dc = DiscoveryCeiling(limit=1)
    dc.note("search_capabilities")
    refused = dc.note("search_capabilities")
    recovered = dc.note("call_tool")
    after = dc.note("search_capabilities")
    return refused == dc.refusal and recovered is None and after is None and dc.count == 1


def check_instances_independent():
    a = DiscoveryCeiling(limit=2)
    b = DiscoveryCeiling(limit=3)
    a.note("list_capabilities")
    a.note("list_capabilities")
    b.note("list_capabilities")
    a_refused = a.note("list_capabilities")
    b_refused = b.note("list_capabilities")
    return (
        a.count == 2
        and b.count == 2
        and a_refused == a.refusal
        and b_refused is None
        and a.refusal != b.refusal
    )


def check_no_shared_class_state():
    a = DiscoveryCeiling(limit=5)
    a.note("describe_tool")
    fresh = DiscoveryCeiling(limit=5)
    return fresh.count == 0 and "count" not in vars(DiscoveryCeiling)


CHECKS = [
    ("defaults", check_defaults),
    ("discovery_increments", check_discovery_increments),
    ("all_discovery_tools_count", check_all_discovery_tools_count),
    ("call_tool_resets", check_call_tool_resets),
    ("refusal_at_limit", check_refusal_at_limit),
    ("count_pinned_on_refusal", check_count_pinned_at_limit_on_refusal),
    ("refusal_text_exact", check_refusal_text_exact),
    ("other_names_inert", check_other_names_inert),
    ("zero_limit_refuses_immediately", check_zero_limit_refuses_immediately),
    ("call_tool_after_refusal_recovers", check_call_tool_after_refusal_recovers),
    ("instances_independent", check_instances_independent),
    ("no_shared_class_state", check_no_shared_class_state),
]


def run_selftest():
    passed = 0
    failed = 0
    for name, fn in CHECKS:
        try:
            ok = bool(fn())
        except Exception as exc:  # a raising check is a failing check
            print("FAIL %s: %r" % (name, exc))
            ok = False
        if ok:
            passed += 1
        else:
            failed += 1
            print("FAIL %s" % name)
    total = len(CHECKS)
    print("tg_ceiling selftest: %d checks, %d passed, %d failed"
          % (total, passed, failed))
    return 0 if (failed == 0 and total > 0) else 1


def main(argv):
    if "--selftest" in argv:
        return run_selftest()
    print(__doc__.strip().splitlines()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
