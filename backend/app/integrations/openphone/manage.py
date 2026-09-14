"""Drive the OpenPhone mirror from the shell. No deploy, no code change.

Examples (inside the app container, exactly like `app.scripts.manage`):

    python -m app.integrations.openphone.manage status
    python -m app.integrations.openphone.manage preview     # size the backfill, WRITE NOTHING
    python -m app.integrations.openphone.manage run         # one tick, for real
    python -m app.integrations.openphone.manage backfill    # force the 30-day pass again
    python -m app.integrations.openphone.manage rows --limit 20
    python -m app.integrations.openphone.manage recordings            # DRY RUN: counts only
    python -m app.integrations.openphone.manage recordings --commit   # ...and repair

`preview` is the one to run first on the production box. It reads OpenPhone and reports
exactly what a backfill WOULD send — no state row, no queued job, nothing to the CRM — which
is how the counts in `.qa/state/openphone-done` are meant to be obtained. It is also the
only command here that can be run before anyone is comfortable turning the mirror on.

EVERY command is read-only against OpenPhone. There is no send, no dial, no contact write,
and no flag that enables one.
"""

import argparse
import asyncio
import json

from sqlalchemy import desc, func, select

from app.db import SessionLocal
from app.integrations.openphone import config as op_config
from app.integrations.openphone import sync as op_sync
from app.integrations.openphone.models import BACKFILL_SETTING_KEY, OpenPhoneMirrorRow
from app.models import AppSetting


async def cmd_status(_args) -> None:
    cfg = op_config.current()
    print("OpenPhone mirror")
    print(f"  enabled            : {cfg.enabled}  (OPENPHONE_MIRROR_ENABLED)")
    print(f"  API key present    : {cfg.api_key_present}")
    print(f"  mode               : polling every {cfg.poll_seconds}s")
    print(f"  backfill window    : {cfg.backfill_days} days, once")
    print(f"  lines included     : {sorted(cfg.include) or '(all on the account)'}")
    print(f"  lines excluded     : {sorted(cfg.exclude) or '(none)'}")
    print(f"  max participants   : {cfg.max_participants} per tick")
    print("  writes to OpenPhone: NEVER — this client issues GET requests only")
    refusal = cfg.refusal()
    print(f"  would run now      : {'no — ' + refusal if refusal else 'yes'}")

    async with SessionLocal() as db:
        row = await db.get(AppSetting, BACKFILL_SETTING_KEY)
        done = (row.value or {}) if row else {}
        print(f"  backfill completed : {done.get('completed_at') or 'not yet'}")
        if done.get("counts"):
            print(f"    counts           : {done['counts']}")
        total = (await db.execute(
            select(OpenPhoneMirrorRow.kind, func.count())
            .group_by(OpenPhoneMirrorRow.kind))).all()
        print(f"  mirrored so far    : {dict(total) or '(nothing)'}")


async def cmd_preview(_args) -> None:
    """Read OpenPhone and report what a backfill WOULD send. Writes nothing, anywhere."""
    result = await op_sync.run_once(dry_run=True, force_backfill=True)
    print(json.dumps(result, indent=2, default=str))
    for src in result.get("participant_sources") or []:
        print("\nparticipants: /conversations %s; %s found (%s from /conversations%s), "
              "%s not E.164 and skipped; ceiling %s"
              % (src.get("conversations"), src.get("participants_found"),
                 src.get("from_conversations"),
                 "" if src.get("conversations") == "ok" else
                 ", %s from the address book, %s from OWEN callers"
                 % (src.get("from_address_book"), src.get("from_owen_callers")),
                 src.get("skipped_not_e164"),
                 "TRUNCATED the set" if src.get("truncated_by_ceiling") else "not reached"))
    # Every error, redacted: no phone number and no query value is ever in these lines.
    for err in result.get("errors") or []:
        if isinstance(err, dict):
            print("ERROR %s: %s participant(s) — %s" % (
                err.get("resource"), err.get("participants"), err.get("error")))
        else:
            print("ERROR:", err)
    if not result.get("complete", True):
        print("\nNOTE: the participant set was INCOMPLETE — /conversations could not be "
              "read, or the max-participants ceiling truncated it (see the line above). "
              "The real backfill is larger than these counts.")


async def cmd_run(_args) -> None:
    print(json.dumps(await op_sync.run_once(), indent=2, default=str))


async def cmd_backfill(_args) -> None:
    print(json.dumps(await op_sync.run_once(force_backfill=True), indent=2, default=str))


async def cmd_rows(args) -> None:
    """The most recent mirrored objects. Shows no customer number in full — the table
    stores only a match key, which is enough to answer 'did this one go?'."""
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(OpenPhoneMirrorRow)
            .order_by(desc(OpenPhoneMirrorRow.pushed_at))
            .limit(args.limit))).scalars().all()
    if not rows:
        print("nothing mirrored yet")
        return
    for r in rows:
        key = r.customer_key or "?"
        print(f"  {r.pushed_at:%Y-%m-%d %H:%M}  {r.kind:8} {r.external_id:24} "
              f"...{key[-4:]:4}  occurred={r.occurred_at}")


async def cmd_recordings(args) -> None:
    """Give already-mirrored calls the recording they were sent without (2026-09-14).

    DRY RUN unless `--commit`. Reads Quo (GET only), prints counts and nothing that names a
    customer. See `recordings.py` for the pacing and why a second run changes nothing."""
    from app.integrations.openphone import recordings

    result = await recordings.repair(commit=args.commit, spacing_seconds=args.spacing)
    print(json.dumps(result, indent=2))
    if not result.get("ran"):
        print("NOT RUN: %s" % result.get("reason"))
        return
    print("%s: checked %d, with audio %d, without %d, already sent %d, errors %d, "
          "enqueued %d" % ("COMMIT" if args.commit else "DRY RUN (nothing enqueued)",
                           result["checked"], result["with_audio"], result["without_audio"],
                           result["already_sent"], result["errors"], result["enqueued"]))
    if result["enqueued"]:
        print("The last job is due in about %d s (jobs are spaced %d s apart for the "
              "agent key's per-minute limit)." % ((result["enqueued"] - 1) * args.spacing,
                                                 args.spacing))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.integrations.openphone.manage")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (
        ("status", cmd_status, "configuration, backfill state and totals"),
        ("preview", cmd_preview, "size the backfill — reads OpenPhone, writes nothing"),
        ("run", cmd_run, "one mirror tick"),
        ("backfill", cmd_backfill, "force the full-window pass again"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=fn)

    p = sub.add_parser("rows", help="recently mirrored objects")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_rows)

    p = sub.add_parser("recordings", help="repair mirrored calls sent without their "
                                          "recording — DRY RUN unless --commit")
    p.add_argument("--commit", action="store_true",
                   help="enqueue the CRM enrichments (default: count only, write nothing)")
    p.add_argument("--spacing", type=int, default=3,
                   help="seconds between queued deliveries (default 3, i.e. 20/min)")
    p.set_defaults(func=cmd_recordings)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
