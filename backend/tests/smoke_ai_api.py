"""Live smoke test for the AI API — run against production after deploying.

Unit tests prove the code does what it was written to do. This proves something the unit tests
structurally cannot: that the AI API and the OWEN dashboard **agree**. Two surfaces answering
"how many calls last week" with different numbers is the worst outcome this feature could
produce — worse than either being unavailable, because nobody would notice.

So the central assertion is a cross-check: `/api/ai/calls/stats` with junk excluded, over the
same window, must equal `/api/dashboard/summary?hide_junk=true`. Everything else here is a
reachability and scope check.

Needs BOTH a user login (for the dashboard, which is JWT-authed) and an API key.

    export OWEN_API_URL=https://api.owen.santiagoproperties.uk
    export OWEN_API_KEY=owen_sk_...
    export OWEN_EMAIL=you@example.com
    export OWEN_PASSWORD=...
    python -m tests.smoke_ai_api
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("OWEN_API_URL", "").rstrip("/")
KEY = os.environ.get("OWEN_API_KEY", "")
EMAIL = os.environ.get("OWEN_EMAIL", "")
PASSWORD = os.environ.get("OWEN_PASSWORD", "")

failures: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def get(path, headers, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def post(path, headers, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def login() -> str | None:
    if not (EMAIL and PASSWORD):
        return None
    data = urllib.parse.urlencode({"username": EMAIL, "password": PASSWORD}).encode()
    req = urllib.request.Request(
        BASE + "/api/auth/login", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def main() -> None:
    if not BASE or not KEY:
        print("set OWEN_API_URL and OWEN_API_KEY", file=sys.stderr)
        sys.exit(2)
    hdr = {"X-OWEN-Key": KEY}

    print("discovery — an AI given only a URL and a key must be able to bootstrap:")
    code, body = get("/api/ai", hdr)
    check("GET /api/ai answers 200", code == 200, str(body)[:200])
    scopes = (body.get("your_key") or {}).get("scopes", []) if code == 200 else []
    check("the index names this key's scopes", bool(scopes), str(scopes))
    check("the index lists endpoints", len(body.get("endpoints") or []) > 8)
    check("the index carries the phantom-row warning up front",
          any("started_at" in n for n in body.get("read_this_first") or []))

    print("\nauth:")
    code, _ = get("/api/ai/calls/stats", {"X-OWEN-Key": "owen_sk_not-a-real-key"})
    check("a bogus key is rejected with 401", code == 401)
    code, _ = get("/api/ai/calls/stats", {})
    check("no key is rejected with 401", code == 401)
    code, _ = get("/api/ai/calls/stats", {"Authorization": f"Bearer {KEY}"})
    check("Authorization: Bearer works as well as X-OWEN-Key", code == 200)

    print("\ncurated metrics:")
    code, calls = get("/api/ai/calls/stats", hdr, {"period": "last_7d"})
    check("GET /api/ai/calls/stats answers 200", code == 200, str(calls)[:300])
    if code == 200:
        check("the response is the standard envelope",
              {"summary", "data", "applied_filters", "notes"} <= set(calls))
        check("applied_filters echoes resolved absolute bounds",
              calls["applied_filters"].get("from", "").startswith("20"))
        check("notes warn about phantom rows",
              any("started_at" in n for n in calls["notes"]))
        check("a daily series comes back", isinstance(calls["data"]["breakdown"], list))

    code, short = get("/api/ai/calls/stats", hdr, {"period": "last_7d", "max_duration": 45})
    check("duration filtering narrows the result",
          code == 200 and short["data"]["total_calls"] <= calls["data"]["total_calls"])

    for path, params in [
        ("/api/ai/leads/stats", {"period": "last_30d"}),
        ("/api/ai/messages/stats", {"period": "last_30d"}),
        ("/api/ai/billing/summary", {"period": "last_30d"}),
        ("/api/ai/calls/top-callers", {"period": "last_30d"}),
        ("/api/ai/calls/categories", {"period": "last_30d"}),
        ("/api/ai/health/pipeline", None),
        ("/api/ai/schema", None),
    ]:
        code, body = get(path, hdr, params)
        check(f"GET {path} answers 200", code == 200, str(body)[:200])

    print("\nbad input is answered usefully, not opaquely:")
    code, body = get("/api/ai/calls/stats", hdr, {"period": "last_fortnight"})
    check("an unknown period returns 400", code == 400)
    check("...naming the valid periods",
          "valid_periods" in (body.get("detail") or {}), str(body)[:200])
    code, body = get("/api/ai/calls/stats", hdr, {"campaign": "definitely-not-a-campaign"})
    check("an unknown campaign returns 404 listing the real ones",
          code == 404 and "known_campaigns" in (body.get("detail") or {}))

    print("\nscope enforcement:")
    code, body = get("/api/ai/errors", hdr, {"since": "1h"})
    if "logs" in scopes:
        check("with the logs scope, /errors answers 200", code == 200, str(body)[:200])
    else:
        check("without the logs scope, /errors answers 403", code == 403)
        check("...naming the missing scope", "scope" in str(body).lower())

    code, body = post("/api/ai/query", hdr, {"sql": "SELECT 1 AS x"})
    if {"sql", "content"} <= set(scopes):
        check("with sql+content, a SELECT runs", code == 200, str(body)[:300])
        code, body = post("/api/ai/query", hdr, {"sql": "DELETE FROM calls"})
        check("a write is refused", code == 400)
        code, body = post("/api/ai/query", hdr, {"sql": "SELECT * FROM users"})
        check("SELECT on `users` is refused BY THE DATABASE ROLE, not by us",
              code == 400 and "permission" in str(body).lower(), str(body)[:300])
        code, body = post("/api/ai/query", hdr, {"sql": "SELECT * FROM api_keys"})
        check("SELECT on `api_keys` is refused too",
              code == 400 and "permission" in str(body).lower(), str(body)[:300])
    else:
        check("without sql+content, /query answers 403 or 503", code in (403, 503))

    print("\nCROSS-CHECK — the AI API and the dashboard must agree:")
    token = login()
    if token is None:
        print("  [SKIP] set OWEN_EMAIL / OWEN_PASSWORD to run the cross-check")
    else:
        jwt = {"Authorization": f"Bearer {token}"}
        # Use the AI API's own resolved bounds so both sides measure the identical window;
        # comparing "last_7d" against a separately-computed range would test nothing.
        af = calls["applied_filters"]
        code, dash = get("/api/dashboard/summary", jwt,
                         {"date_from": af["from"], "date_to": af["to"], "hide_junk": "true"})
        check("dashboard summary answers 200", code == 200, str(dash)[:200])
        if code == 200:
            check(f"total_calls matches the dashboard "
                  f"(ai={calls['data']['total_calls']} dashboard={dash['total_calls']})",
                  calls["data"]["total_calls"] == dash["total_calls"])
            check(f"junk count matches "
                  f"(ai={calls['data']['junk_calls_in_window']} dashboard={dash['junk_calls']})",
                  calls["data"]["junk_calls_in_window"] == dash["junk_calls"])
            check(f"new-for-campaign matches "
                  f"(ai={calls['data']['new_for_campaign']} dashboard={dash['new_for_campaign']})",
                  calls["data"]["new_for_campaign"] == dash["new_for_campaign"])

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL AI API SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
