#!/usr/bin/env python3
"""`owen` — a thin command-line wrapper over the OWEN AI API.

For agents that have a shell but no convenient HTTP client, and for humans debugging
production. It is deliberately thin: every subcommand maps to exactly one endpoint and does no
computation of its own, so the CLI can never disagree with the API about what a number means.

Standard library only — no install step, no virtualenv, nothing to keep in sync with the
backend's requirements. Copy this file anywhere Python 3.9+ runs.

Configuration, in precedence order:
    1. --url / --key flags
    2. OWEN_API_URL / OWEN_API_KEY environment variables
    3. ~/.owen/config.json  ->  {"url": "https://...", "key": "owen_sk_..."}

Output is JSON by default because the primary caller is a machine; `--table` renders the
`summary` line and a readable table for humans.

Exit codes: 0 success · 1 API error (the error body is printed) · 2 usage/config error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".owen" / "config.json"
TIMEOUT = 60


# --- config --------------------------------------------------------------------------
def load_config(url: str | None, key: str | None) -> tuple[str, str]:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"could not read {CONFIG_PATH}: {exc}", code=2)
    resolved_url = url or os.environ.get("OWEN_API_URL") or cfg.get("url")
    resolved_key = key or os.environ.get("OWEN_API_KEY") or cfg.get("key")
    if not resolved_url or not resolved_key:
        fail(
            "OWEN API URL and key are required.\n"
            "  export OWEN_API_URL=https://api.owen.santiagoproperties.uk\n"
            "  export OWEN_API_KEY=owen_sk_...\n"
            f"or write {CONFIG_PATH} as {{\"url\": \"...\", \"key\": \"...\"}}\n"
            "Issue a key in the OWEN UI under API Keys.",
            code=2,
        )
    return resolved_url.rstrip("/"), resolved_key


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


# --- transport -----------------------------------------------------------------------
def call(url: str, key: str, path: str, params: dict | None = None,
         body: dict | None = None, raw: bool = False):
    """One request. Error bodies are surfaced verbatim — they carry the `hint` field that
    tells the caller what to do next, which is the whole point of them."""
    target = f"{url}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            target += "?" + urllib.parse.urlencode(
                {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in clean.items()}
            )
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        target, data=data, method="POST" if body is not None else "GET",
        headers={
            "X-OWEN-Key": key,
            "Accept": "text/markdown" if raw else "application/json",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            detail = json.dumps(parsed.get("detail", parsed), indent=2)
        except json.JSONDecodeError:
            pass
        fail(f"HTTP {exc.code} from {path}:\n{detail}")
    except urllib.error.URLError as exc:
        fail(f"could not reach {url}: {exc.reason}", code=2)
    return payload if raw else json.loads(payload)


# --- rendering -----------------------------------------------------------------------
def render(result: dict, as_table: bool) -> None:
    if not as_table:
        print(json.dumps(result, indent=2, default=str))
        return

    if isinstance(result, dict) and "summary" in result:
        print(result["summary"])
        for note in result.get("notes") or []:
            print(f"  note: {note}")
        print()
        data = result.get("data") or {}
    else:
        data = result

    scalars = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
    if scalars:
        width = max(len(k) for k in scalars)
        for k, v in scalars.items():
            print(f"  {k:<{width}}  {v}")
        print()

    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"  {k}:")
            _print_rows(v)
            print()
        elif isinstance(v, dict):
            print(f"  {k}:")
            for kk, vv in v.items():
                print(f"    {kk}: {vv}")
            print()


def _print_rows(rows: list[dict]) -> None:
    cols: list[str] = []
    for row in rows:
        for c in row:
            if c not in cols:
                cols.append(c)
    widths = {
        c: max(len(c), *(len(_cell(r.get(c))) for r in rows)) for c in cols
    }
    print("    " + "  ".join(c.ljust(widths[c]) for c in cols))
    print("    " + "  ".join("-" * widths[c] for c in cols))
    for row in rows:
        print("    " + "  ".join(_cell(row.get(c)).ljust(widths[c]) for c in cols))


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)[:60]
    return str(value)


# --- commands ------------------------------------------------------------------------
def add_period_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--period", help="today | yesterday | last_7d | this_week | last_month | ...")
    p.add_argument("--from", dest="date_from", help="ISO date/time (business timezone)")
    p.add_argument("--to", dest="date_to", help="ISO date/time (business timezone)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="owen",
        description="Query the OWEN call-attribution platform. Read-only.",
        epilog="Run `owen docs` for the full manual, including every filter and worked examples.",
    )
    p.add_argument("--url", help="API base URL (default: $OWEN_API_URL)")
    p.add_argument("--key", help="API key (default: $OWEN_API_KEY)")
    p.add_argument("--table", action="store_true", help="human-readable output (default: JSON)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("index", help="Machine-readable index of every endpoint")
    sub.add_parser("docs", help="Print the full API manual (Markdown)")
    sub.add_parser("health", help="Is anything broken right now")

    c = sub.add_parser("calls", help="Call volume and duration stats")
    add_period_args(c)
    c.add_argument("--min-duration", type=int, help="seconds, inclusive")
    c.add_argument("--max-duration", type=int, help="seconds, inclusive ('under 45s' = 45)")
    c.add_argument("--campaign")
    c.add_argument("--number", help="tracking number in E.164")
    c.add_argument("--direction", choices=["inbound", "outbound"])
    c.add_argument("--status")
    c.add_argument("--answered", choices=["true", "false"])
    c.add_argument("--new-callers", choices=["true", "false"])
    c.add_argument("--include-junk", action="store_true",
                   help="also count <=13s and never-connected calls")
    c.add_argument("--group-by", default="day",
                   choices=["day", "hour_of_day", "campaign", "number", "status", "none"])

    r = sub.add_parser("recent", help="Individual calls with AI summaries [scope: content]")
    add_period_args(r)
    r.add_argument("--min-duration", type=int)
    r.add_argument("--max-duration", type=int)
    r.add_argument("--include-junk", action="store_true")
    r.add_argument("--limit", type=int, default=25)

    t = sub.add_parser("transcript", help="Full transcript of one call [scope: content]")
    t.add_argument("call_id")

    tc = sub.add_parser("top-callers", help="Who called most")
    add_period_args(tc)
    tc.add_argument("--limit", type=int, default=20)
    tc.add_argument("--include-junk", action="store_true")

    cat = sub.add_parser("categories", help="AI category mix for analyzed calls")
    add_period_args(cat)

    lead = sub.add_parser("leads", help="New leads from job-notification emails")
    add_period_args(lead)
    lead.add_argument("--source", help="e.g. dispatch")
    lead.add_argument("--group-by", default="day", choices=["day", "week", "source", "brand", "none"])

    lr = sub.add_parser("leads-recent", help="Individual leads with details [scope: content]")
    add_period_args(lr)
    lr.add_argument("--source")
    lr.add_argument("--parse-status", default="parsed", choices=["parsed", "failed", "all"])
    lr.add_argument("--limit", type=int, default=25)

    m = sub.add_parser("messages", help="SMS/MMS volume")
    add_period_args(m)
    m.add_argument("--direction", choices=["inbound", "outbound"])
    m.add_argument("--group-by", default="day", choices=["day", "direction", "none"])

    b = sub.add_parser("billing", help="Telephony spend")
    add_period_args(b)
    b.add_argument("--group-by", default="day",
                   choices=["day", "number", "kind", "direction", "none"])

    e = sub.add_parser("errors", help="Errors, dead jobs and failed relays [scope: logs]")
    e.add_argument("--since", default="24h", help="30m | 6h | 7d")
    e.add_argument("--source", help="logs | jobs | emails (comma-separated)")
    e.add_argument("--level", choices=["WARNING", "ERROR", "CRITICAL"])
    e.add_argument("--service", choices=["app", "worker"])
    e.add_argument("--linkedid")
    e.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("schema", help="Database schema for writing SQL")
    s.add_argument("--table", dest="table_name", help="one table only")

    q = sub.add_parser("query", help="Run read-only SQL [scopes: sql + content]")
    q.add_argument("sql", help="a single SELECT/WITH statement; '-' reads stdin")
    q.add_argument("--limit", type=int)

    return p


def _tri(value: str | None) -> bool | None:
    """argparse gives us 'true'/'false'/None; the API wants a real tristate."""
    return None if value is None else value == "true"


def main() -> None:
    args = build_parser().parse_args()
    url, key = load_config(args.url, args.key)
    cmd = args.cmd
    period = {"period": getattr(args, "period", None),
              "date_from": getattr(args, "date_from", None),
              "date_to": getattr(args, "date_to", None)}

    if cmd == "docs":
        print(call(url, key, "/api/ai/docs", raw=True))
        return

    if cmd == "index":
        result = call(url, key, "/api/ai")
    elif cmd == "health":
        result = call(url, key, "/api/ai/health/pipeline")
    elif cmd == "calls":
        result = call(url, key, "/api/ai/calls/stats", {
            **period, "min_duration": args.min_duration, "max_duration": args.max_duration,
            "campaign": args.campaign, "number": args.number, "direction": args.direction,
            "status": args.status, "answered": _tri(args.answered),
            "new_callers": _tri(args.new_callers), "include_junk": args.include_junk,
            "group_by": args.group_by,
        })
    elif cmd == "recent":
        result = call(url, key, "/api/ai/calls/recent", {
            **period, "min_duration": args.min_duration, "max_duration": args.max_duration,
            "include_junk": args.include_junk, "limit": args.limit,
        })
    elif cmd == "transcript":
        result = call(url, key, f"/api/ai/calls/{args.call_id}/transcript")
    elif cmd == "top-callers":
        result = call(url, key, "/api/ai/calls/top-callers",
                      {**period, "limit": args.limit, "include_junk": args.include_junk})
    elif cmd == "categories":
        result = call(url, key, "/api/ai/calls/categories", period)
    elif cmd == "leads":
        result = call(url, key, "/api/ai/leads/stats",
                      {**period, "source": args.source, "group_by": args.group_by})
    elif cmd == "leads-recent":
        result = call(url, key, "/api/ai/leads/recent",
                      {**period, "source": args.source, "parse_status": args.parse_status,
                       "limit": args.limit})
    elif cmd == "messages":
        result = call(url, key, "/api/ai/messages/stats",
                      {**period, "direction": args.direction, "group_by": args.group_by})
    elif cmd == "billing":
        result = call(url, key, "/api/ai/billing/summary", {**period, "group_by": args.group_by})
    elif cmd == "errors":
        result = call(url, key, "/api/ai/errors", {
            "since": args.since, "source": args.source, "level": args.level,
            "service": args.service, "linkedid": args.linkedid, "limit": args.limit,
        })
    elif cmd == "schema":
        result = call(url, key, "/api/ai/schema", {"table": args.table_name})
    elif cmd == "query":
        sql = sys.stdin.read() if args.sql == "-" else args.sql
        result = call(url, key, "/api/ai/query", body={"sql": sql, "limit": args.limit})
    else:  # pragma: no cover - argparse rejects anything else
        fail(f"unknown command {cmd!r}", code=2)

    render(result, args.table)


if __name__ == "__main__":
    main()
