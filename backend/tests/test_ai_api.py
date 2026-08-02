"""Unit tests for the AI API (app/api/ai/*).

No DB, no network. Covers the four places this feature can be quietly, expensively wrong:

1. **Period resolution.** "Today" must mean today in Miami, not in UTC — and must stay correct
   across both DST transitions. A silent one-hour skew puts evening calls on the wrong day and
   makes every daily figure slightly untrue.
2. **Call predicates.** `started_at IS NOT NULL` must be non-negotiable, and the junk rule must
   be the same object the dashboard uses, so the two can never disagree.
3. **The SQL guard.** Writes, statement chaining and non-queries must be refused with a usable
   message, before the database role has to refuse them with an opaque one.
4. **Key handling.** Hashing, scope normalization, and header extraction from both accepted
   forms.

Run: python -m tests.test_ai_api
"""

import pathlib
import sys
from datetime import datetime, timezone

from app.api.ai import periods
from app.api.ai.envelope import error_detail, ok
from app.api.ai.filters import REAL_CALL, call_filters
from app.api.junk import IS_JUNK, NOT_JUNK
from app.core import apikeys


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"ai api failed at: {name}")


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# --- 1. periods ----------------------------------------------------------------------
def test_periods():
    print("periods — resolved in America/New_York:")

    # 2026-08-01 02:00 UTC is still 2026-07-31 22:00 in Miami (EDT, UTC-4). "Today" must
    # therefore start at Jul 31 04:00 UTC, not Aug 1 00:00 UTC. This is the exact bug that
    # would move every late-evening call into the next day.
    now = _utc(2026, 8, 1, 2)
    start, end, desc = periods.resolve("today", now=now)
    check("today starts at business-local midnight, not UTC midnight",
          start == _utc(2026, 7, 31, 4))
    check("today ends at now", end == now)
    check("described period echoes the name", desc["period"] == "today")
    check("described bounds are absolute UTC", desc["from"] == start.isoformat())

    start, end, _ = periods.resolve("yesterday", now=now)
    check("yesterday is a full local day", start == _utc(2026, 7, 30, 4) and end == _utc(2026, 7, 31, 4))

    # Winter: EST is UTC-5, so local midnight is 05:00 UTC. Same code, different offset —
    # this is what proves the boundary is localized rather than arithmetic on UTC.
    winter = _utc(2026, 1, 15, 2)
    start, _, _ = periods.resolve("today", now=winter)
    check("DST-aware: in January local midnight is 05:00 UTC", start == _utc(2026, 1, 14, 5))

    # 2026-08-01 is a Saturday; the week's Monday is 2026-07-27.
    start, _, _ = periods.resolve("this_week", now=_utc(2026, 8, 1, 16))
    check("this_week starts Monday local midnight", start == _utc(2026, 7, 27, 4))

    start, end, _ = periods.resolve("last_week", now=_utc(2026, 8, 1, 16))
    check("last_week is the previous Mon-Sun, half-open",
          start == _utc(2026, 7, 20, 4) and end == _utc(2026, 7, 27, 4))

    start, end, _ = periods.resolve("last_month", now=_utc(2026, 8, 1, 16))
    check("last_month is the whole previous calendar month",
          start == _utc(2026, 7, 1, 4) and end == _utc(2026, 8, 1, 4))

    # March has 31 days; stepping back from Apr 1 must land on Mar 1, not Mar 2.
    start, end, _ = periods.resolve("last_month", now=_utc(2026, 4, 10, 16))
    check("last_month handles month lengths", start == _utc(2026, 3, 1, 5))

    start, _, _ = periods.resolve("this_month", now=_utc(2026, 8, 15, 16))
    check("mtd is an alias of this_month",
          periods.resolve("mtd", now=_utc(2026, 8, 15, 16))[0] == start)

    start, _, _ = periods.resolve("all_time", now=now)
    check("all_time has no lower bound", start is None)

    start, _, _ = periods.resolve(None, now=now)
    check("default period is last_7d", start == now - __import__("datetime").timedelta(days=7))

    try:
        periods.resolve("last_fortnight", now=now)
        check("unknown period raises", False)
    except ValueError:
        check("unknown period raises ValueError (so the route can list valid ones)", True)

    # An explicit naive date means the local day, which is what a person writing "2026-07-01"
    # means. Interpreting it as UTC would shift the window by the offset.
    start, _, desc = periods.resolve(None, date_from=datetime(2026, 7, 1), now=now)
    check("naive date_from is business-local", start == _utc(2026, 7, 1, 4))
    check("explicit dates report as custom", desc["period"] == "custom")

    start, _, _ = periods.resolve(None, date_from=_utc(2026, 7, 1), now=now)
    check("aware date_from is respected as given", start == _utc(2026, 7, 1))

    check("describe_window is human-readable",
          "America/New_York" in periods.describe_window(desc))


# --- 2. call predicates --------------------------------------------------------------
def test_filters():
    print("\ncall filters — what counts as a call:")

    where = call_filters()
    check("REAL_CALL is always applied, even with no arguments", where[0] is REAL_CALL)
    check("junk is excluded by default", any(c is NOT_JUNK for c in where))

    where = call_filters(include_junk=True)
    check("include_junk drops the junk exclusion", not any(c is NOT_JUNK for c in where))
    check("include_junk does NOT drop the phantom-row filter", where[0] is REAL_CALL)

    # The dashboard imports these same objects. Identity (not equality) is the point: if this
    # ever becomes a local copy, the two surfaces can drift apart without anything failing.
    from app.api.ai import filters as f
    check("junk predicate is shared with the dashboard, not reimplemented",
          f.NOT_JUNK is NOT_JUNK and f.IS_JUNK is IS_JUNK)

    where = call_filters(max_duration=45)
    check("max_duration adds a bound", len(where) == 3)
    sql = str(call_filters(max_duration=45)[-1].compile())
    check("max_duration is inclusive ('under 45s' means <= 45)", "<=" in sql)
    check("max_duration excludes NULL durations (unknown is not 'under')", "IS NOT NULL" in sql)

    sql = str(call_filters(answered=True)[-1].compile())
    check("answered=True keys off answered_at, not the status string", "answered_at" in sql)

    n = len(call_filters(campaign_id="x", number_id="y", direction="inbound", status="completed",
                         new_callers=True))
    check("every filter contributes exactly one clause", n == 2 + 5)


# --- 3. SQL guard --------------------------------------------------------------------
def test_sql_guard():
    print("\nSQL guard — refusals happen before the DB role has to refuse:")
    from fastapi import HTTPException

    from app.api.ai.query import _validate

    def rejects(sql, expect_code):
        try:
            _validate(sql)
            return False
        except HTTPException as exc:
            return exc.detail.get("error") == expect_code and "hint" in exc.detail

    check("SELECT is accepted", _validate("SELECT 1") == "SELECT 1")
    check("WITH (CTE) is accepted", _validate("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH"))
    check("EXPLAIN is accepted", _validate("EXPLAIN SELECT 1").startswith("EXPLAIN"))
    check("a trailing semicolon is tolerated", _validate("SELECT 1;") == "SELECT 1")

    check("INSERT is refused", rejects("INSERT INTO calls VALUES (1)", "not_read_only"))
    check("UPDATE is refused", rejects("UPDATE calls SET status='x'", "not_read_only"))
    check("DELETE is refused", rejects("DELETE FROM calls", "not_read_only"))
    check("DROP is refused", rejects("DROP TABLE calls", "not_read_only"))
    check("GRANT is refused", rejects("GRANT ALL ON calls TO owen_ro", "not_read_only"))
    check("leading whitespace does not evade the check", rejects("   delete from calls", "not_read_only"))
    check("case does not evade the check", rejects("DeLeTe FROM calls", "not_read_only"))

    check("statement chaining is refused",
          rejects("SELECT 1; DROP TABLE calls", "multiple_statements"))
    check("chaining is caught even when the first statement is a valid read",
          rejects("SELECT 1; SELECT 2", "multiple_statements"))

    check("a non-query is refused with guidance", rejects("VACUUM", "not_read_only"))
    check("gibberish is refused", rejects("hello world", "not_a_query"))

    # Errors must be instructive: a machine caller cannot ask a follow-up question.
    from fastapi import HTTPException as HE
    try:
        _validate("DELETE FROM calls")
    except HE as exc:
        check("refusals carry an actionable hint", "read-only" in exc.detail["hint"].lower())


# --- 4. keys -------------------------------------------------------------------------
def test_keys():
    print("\nAPI keys:")
    k1, k2 = apikeys.generate_key(), apikeys.generate_key()
    check("keys carry an identifying prefix", k1.startswith("owen_sk_"))
    check("keys are unique", k1 != k2)
    check("keys carry real entropy (>=32 chars of secret)", len(k1) - len("owen_sk_") >= 32)

    check("hashing is deterministic", apikeys.hash_key(k1) == apikeys.hash_key(k1))
    check("different keys hash differently", apikeys.hash_key(k1) != apikeys.hash_key(k2))
    check("the hash does not contain the plaintext", k1 not in apikeys.hash_key(k1))
    check("display prefix is a short fragment, not the key",
          apikeys.display_prefix(k1) in k1 and len(apikeys.display_prefix(k1)) < len(k1))

    check("scopes default to read-only metrics", apikeys.normalize_scopes(None) == ["read"])
    check("unknown scopes are dropped, not honoured",
          apikeys.normalize_scopes(["read", "admin", "write"]) == ["read"])
    check("scope order is stable regardless of input order",
          apikeys.normalize_scopes(["sql", "read"]) == apikeys.normalize_scopes(["read", "sql"]))
    check("scopes are de-duplicated", apikeys.normalize_scopes(["read", "read"]) == ["read"])
    check("scope matching is case-insensitive", apikeys.normalize_scopes(["READ"]) == ["read"])

    check("X-OWEN-Key header is read", apikeys.extract_key(None, k1) == k1)
    check("Authorization: Bearer is read", apikeys.extract_key(f"Bearer {k1}", None) == k1)
    check("bearer scheme is case-insensitive", apikeys.extract_key(f"bearer {k1}", None) == k1)
    check("a user JWT on Authorization is NOT mistaken for an API key",
          apikeys.extract_key("Bearer eyJhbGciOiJIUzI1NiJ9.abc.def", None) is None)
    check("no credential presented returns None", apikeys.extract_key(None, None) is None)
    check("X-OWEN-Key wins when both are present",
          apikeys.extract_key(f"Bearer {k2}", k1) == k1)


# --- 5. envelope ---------------------------------------------------------------------
def test_envelope():
    print("\nresponse envelope:")
    r = ok("42 calls.", {"total": 42}, {"period": "today"}, ["a caveat"])
    check("envelope always has all four fields",
          set(r) == {"summary", "data", "applied_filters", "notes"})
    check("notes survive", r["notes"] == ["a caveat"])
    r = ok("x", {})
    check("filters and notes default to empty, never missing",
          r["applied_filters"] == {} and r["notes"] == [])

    e = error_detail("bad_thing", "It broke.", hint="Do this instead.", valid=["a"])
    check("errors name a machine-readable code", e["error"] == "bad_thing")
    check("errors carry a hint", e["hint"] == "Do this instead.")
    check("errors can carry the valid values", e["valid"] == ["a"])


# --- 6. regressions ------------------------------------------------------------------
def test_regressions():
    """Three defects found by auditing the first deploy against production."""
    print("\nregressions:")
    from fastapi import HTTPException

    from app.api.ai import content, metrics
    from app.api.ai.deps import resolve_window

    # 1. The content endpoints called periods.resolve directly, so a typo'd period raised
    #    ValueError and became a 500 — while /calls/stats returned a helpful 400.
    try:
        resolve_window("last_fortnight", None, None)
        check("a bad period raises HTTPException, not ValueError", False)
    except HTTPException as exc:
        check("a bad period is a 400, not a 500", exc.status_code == 400)
        check("...and lists the valid periods", "valid_periods" in exc.detail)
    except ValueError:
        check("a bad period is a 400, not a 500 (still raising ValueError)", False)

    src = pathlib.Path(content.__file__).read_text(encoding="utf-8")
    check("content endpoints go through the shared resolver",
          "periods.resolve(" not in src and "resolve_window(" in src)
    src = pathlib.Path(metrics.__file__).read_text(encoding="utf-8")
    check("metric endpoints go through the same resolver",
          "periods.resolve(" not in src and "resolve_window(" in src)

    # 2. junk_calls was computed over the whole window, ignoring campaign/number filters —
    #    so a single campaign's stats reported the account-wide junk figure beside its own
    #    total, inviting a wrong comparison ("Craigslist: 69 calls, 751 junk").
    # REAL_CALL + max_duration + campaign_id = 3 clauses (no window passed here).
    scoped = call_filters(campaign_id="c", max_duration=45, include_junk=True)
    check("the junk count carries the same scoping filters as the total",
          len(scoped) == 3 and scoped[0] is REAL_CALL)
    check("...but drops the junk exclusion itself",
          not any(c is NOT_JUNK for c in scoped))
    check("junk_calls_matching_filters is the reported field name",
          "junk_calls_matching_filters" in pathlib.Path(metrics.__file__).read_text(encoding="utf-8"))

    # 3. Only /query recorded api_key_usage, so a read-only key showed "0 requests in 24h"
    #    in the UI no matter how heavily it was used.
    from app.api.ai import deps
    check("a usage middleware exists to audit every AI request",
          hasattr(deps, "usage_middleware"))
    src = pathlib.Path(deps.__file__).read_text(encoding="utf-8")
    check("...scoped to /api/ai so it never touches other routes",
          'startswith("/api/ai")' in src)
    check("...and skips requests a route already recorded (no double-count)",
          "ai_usage_recorded" in src)
    import app.main as main_mod
    check("the middleware is actually registered on the app",
          any("usage_middleware" in str(getattr(mw, "kwargs", {})) or
              "usage_middleware" in repr(mw) for mw in main_mod.app.user_middleware)
          or any(getattr(f, "__name__", "") == "usage_middleware"
                 for f in [getattr(mw, "cls", None) for mw in main_mod.app.user_middleware]
                 if f) or _middleware_registered(main_mod))


def _middleware_registered(main_mod) -> bool:
    """Starlette wraps @app.middleware('http') functions in BaseHTTPMiddleware; the function
    itself is buried in the options, so look for it by name across the whole repr."""
    return "usage_middleware" in repr(main_mod.app.user_middleware)


def run():
    test_periods()
    test_filters()
    test_sql_guard()
    test_keys()
    test_envelope()
    test_regressions()
    print("\nALL AI API CHECKS PASSED")


if __name__ == "__main__":
    try:
        run()
    except SystemExit as e:
        print(e)
        sys.exit(1)
