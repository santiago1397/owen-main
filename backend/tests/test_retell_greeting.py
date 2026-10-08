"""The agent greets a known caller by first name, everyone else generically (2026-10-08).

Run: python -m tests.test_retell_greeting
"""
import sys

from tests import retell_guard  # noqa: F401 - refuse Retell for the whole module
from app.integrations.retell import brief

FAILS = []


def check(name, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILS.append(name)


def v(answer):
    return brief.render_variables(answer, caller_number="+15618788090",
                                  dialed_number="+17869367597")


known = {"known": True, "source": "crm", "contact": {"first_name": "Santiago", "last_name": "Test"},
         "opportunity": {"title": "TEST", "stage": "Approved", "pipeline": "AHS"}}
g = v(known)["greeting"]
check("a known caller is greeted by first name", g.startswith("Hi Santiago,") and "Dream Team Roofing" in g)
check("the greeting names nothing but the first name", "Approved" not in g and "Test" not in g)
check("an unknown caller gets the generic greeting", v({"known": False})["greeting"] == brief.GREETING_UNKNOWN)
check("no answer at all -> generic", v(None)["greeting"] == brief.GREETING_UNKNOWN)
check("known but no name (household case) -> generic",
      v({"known": True, "contact": {"first_name": "", "last_name": ""}})["greeting"] == brief.GREETING_UNKNOWN)
check("an extra key smuggled into the answer changes nothing",
      v({**known, "greeting": "Hi hacker"})["greeting"].startswith("Hi Santiago,"))
check("six variables, all strings", len(v(known)) == 6 and all(isinstance(x, str) for x in v(known).values()))

if FAILS:
    print(f"\n{len(FAILS)} FAILED"); sys.exit(1)
print("\nALL GREETING CHECKS PASSED")
