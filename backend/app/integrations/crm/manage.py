"""Bind and unbind numbers, from the shell. No deploy, no code change.

The owner's explicit requirement: "changing which number is linked must not require a code
change". Every routing decision this module makes reads the `crm_links` row these commands
write, so binding a different DID is one command and a call.

Examples (inside the app container, exactly like `app.scripts.manage`):

    python -m app.integrations.crm.manage list
    python -m app.integrations.crm.manage bind --phone +15615550100 \\
        --pstn +15615550111 --pstn +15615550122 --operator owner@dreamteamroofingfl.com
    python -m app.integrations.crm.manage enable  --phone +15615550100
    python -m app.integrations.crm.manage disable --phone +15615550100
    python -m app.integrations.crm.manage unbind  --phone +15615550100

`bind` creates the row DISABLED. Enabling is a second, deliberate command, because the
number in question rings in a real business and the review step is the point.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.db import SessionLocal
from app.integrations.crm import config as crm_config
from app.integrations.crm.models import CrmLink
from app.models import Number


async def _number(db, phone: str) -> Number:
    row = (
        await db.execute(
            select(Number).where(
                Number.phone_number == phone,
                Number.media_provider == settings.BULKVS_MEDIA_PROVIDER,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise SystemExit(
            f"no Asterisk-media number {phone!r} — run the BulkVS sync, or check the E.164 form"
        )
    return row


async def _link(db, phone: str) -> tuple[Number, CrmLink]:
    number = await _number(db, phone)
    link = (
        await db.execute(select(CrmLink).where(CrmLink.number_id == number.id))
    ).scalar_one_or_none()
    if link is None:
        raise SystemExit(f"{phone} is not bound — run `bind` first")
    return number, link


async def cmd_bind(args) -> None:
    cfg = crm_config.current()
    allowed, refused = cfg.filter_pstn(args.pstn)
    for num, reason in refused:
        # Stored anyway: the binding is routing INTENT, and the allowlist is a separate,
        # deliberately re-checked permission. Saying so now beats a silent no-ring later.
        print(f"  WARNING  {num} would be refused at call time — {reason}")

    async with SessionLocal() as db:
        number = await _number(db, args.phone)
        link = (
            await db.execute(select(CrmLink).where(CrmLink.number_id == number.id))
        ).scalar_one_or_none()
        created = link is None
        if link is None:
            link = CrmLink(number_id=number.id, enabled=False)
            db.add(link)
        link.ring_operators = not args.no_operators
        link.operator_ids = list(args.operator or [])
        link.pstn_numbers = [crm_config.to_e164(p) for p in (args.pstn or [])]
        if args.ring_timeout is not None:
            link.ring_timeout_seconds = int(args.ring_timeout)
        if args.crm_base_url:
            link.crm_base_url = args.crm_base_url
        if args.crm_token_env:
            link.crm_token_env = args.crm_token_env
        if args.outbound_operator:
            link.outbound_operator = args.outbound_operator
        if args.note:
            link.note = args.note
        await db.commit()

    print(f"{'bound' if created else 'updated'}: {args.phone}")
    print(f"  ring operators : {not args.no_operators} "
          f"({', '.join(args.operator) if args.operator else 'every AVAILABLE operator'})")
    print(f"  PSTN legs      : {', '.join(args.pstn) if args.pstn else '(none)'}")
    print(f"  allowlisted now: {', '.join(allowed) if allowed else '(none)'}")
    if created:
        print(f"\n  The row is DISABLED. Review it, then: "
              f"python -m app.integrations.crm.manage enable --phone {args.phone}")


async def _set_enabled(phone: str, value: bool) -> None:
    async with SessionLocal() as db:
        _number, link = await _link(db, phone)
        link.enabled = value
        await db.commit()
    print(f"{phone}: enabled={value}")
    if value and not crm_config.link_enabled():
        print("  NOTE  CRM_LINK_ENABLED is false, so this binding still does nothing.")


async def cmd_enable(args) -> None:
    await _set_enabled(args.phone, True)


async def cmd_disable(args) -> None:
    await _set_enabled(args.phone, False)


async def cmd_unbind(args) -> None:
    async with SessionLocal() as db:
        _number, link = await _link(db, args.phone)
        await db.delete(link)
        await db.commit()
    print(f"unbound: {args.phone}")


async def cmd_list(_args) -> None:
    cfg = crm_config.current()
    print(f"CRM_LINK_ENABLED = {cfg.enabled}    SMS = {cfg.sms_enabled}    "
          f"allowlist = {len(cfg.allowlist)} destination(s)")
    print(f"CRM base URL     = {cfg.base_url or '(unset)'}    "
          f"token = {'set' if cfg.token else 'UNSET'}")
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(CrmLink, Number).join(Number, Number.id == CrmLink.number_id)
                .order_by(Number.phone_number)
            )
        ).all()
    if not rows:
        print("\n(no bindings)")
        return
    print()
    for link, number in rows:
        allowed, refused = cfg.filter_pstn(link.pstn_numbers or [])
        print(f"{number.phone_number}  enabled={link.enabled}  "
              f"({number.friendly_name or 'no friendly name'})")
        print(f"    operators : {'off' if not link.ring_operators else (', '.join(link.operator_ids or []) or 'every AVAILABLE')}")
        print(f"    pstn      : {', '.join(link.pstn_numbers or []) or '(none)'}")
        print(f"    will ring : {', '.join(allowed) or '(none)'}"
              + (f"   REFUSED: {', '.join(n for n, _ in refused)}" if refused else ""))
        print(f"    timeout   : {link.ring_timeout_seconds or cfg.ring_timeout_seconds}s"
              f"   outbound operator: {link.outbound_operator or '(none)'}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.integrations.crm.manage",
                                     description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bind", help="create or update a binding (created DISABLED)")
    p.add_argument("--phone", required=True, help="the DID, E.164")
    p.add_argument("--pstn", action="append", default=[],
                   help="a PSTN number to ring in parallel (repeatable, max 2)")
    p.add_argument("--operator", action="append", default=[],
                   help="operator id to ring (repeatable; omit = every AVAILABLE operator)")
    p.add_argument("--no-operators", action="store_true", help="ring PSTN only")
    p.add_argument("--ring-timeout", type=int, default=None)
    p.add_argument("--crm-base-url", default=None, help="override CRM_LINK_BASE_URL")
    p.add_argument("--crm-token-env", default=None,
                   help="NAME of the env var holding this CRM's token (never the token)")
    p.add_argument("--outbound-operator", default=None,
                   help="operator rung first for a CRM-initiated outbound call")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_bind)

    for name, fn, helptext in (
        ("enable", cmd_enable, "switch a binding on"),
        ("disable", cmd_disable, "switch a binding off (keeps the row)"),
        ("unbind", cmd_unbind, "delete the binding"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--phone", required=True)
        p.set_defaults(func=fn)

    p = sub.add_parser("list", help="show configuration and every binding")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
