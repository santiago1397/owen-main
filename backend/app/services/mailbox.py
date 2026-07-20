"""IMAP mailbox client (Hostinger). Stdlib only (imaplib + email) — no extra deps.

All functions here are BLOCKING (imaplib is synchronous); the poller runs them off the
event loop via asyncio.to_thread. We connect over SSL, search only for mail from the
configured sender (so other mail in the box is never touched), and pull the raw RFC822
bytes so the full email can be archived. Quoted-printable / base64 transfer encodings are
decoded by the stdlib email parser when we read each part's payload.
"""

import email
import imaplib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message as PyMessage
from email.utils import parsedate_to_datetime, parseaddr

logger = logging.getLogger("mailbox")


@dataclass
class FetchedEmail:
    uid: bytes
    message_id: str
    from_addr: str
    to_addr: str
    subject: str
    received_at: datetime
    text_body: str
    html_body: str
    raw: str


def _hdr(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a malformed header must never crash the poll
        return value


def _body_parts(msg: PyMessage) -> tuple[str, str]:
    """Return (text_plain, text_html), each decoded to a str (best-effort charset)."""
    text_plain, text_html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)  # decodes QP / base64
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, TypeError):
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            text_plain += text
        else:
            text_html += text
    return text_plain, text_html


def fetch_from_sender(
    host: str, port: int, user: str, password: str, folder: str,
    sender: str, batch: int,
) -> list[FetchedEmail]:
    """Fetch up to `batch` UNSEEN messages from `sender`. Read-only w.r.t. flags (we mark
    \\Seen separately, only after the message is safely persisted)."""
    conn = imaplib.IMAP4_SSL(host, port)
    try:
        conn.login(user, password)
        conn.select(folder)  # read-write so a later mark_seen on the same box works
        typ, data = conn.uid("SEARCH", None, "UNSEEN", "FROM", f'"{sender}"')
        if typ != "OK":
            logger.warning("mailbox: SEARCH failed: %s", typ)
            return []
        uids = data[0].split()
        if not uids:
            return []
        uids = uids[:batch]
        out: list[FetchedEmail] = []
        for uid in uids:
            typ, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: don't set \Seen
            if typ != "OK" or not msg_data or not msg_data[0]:
                logger.warning("mailbox: FETCH failed for uid %s", uid)
                continue
            raw_bytes = msg_data[0][1]
            msg = email.message_from_bytes(raw_bytes)
            try:
                received = parsedate_to_datetime(msg.get("Date"))
                if received is not None and received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                received = None
            text_plain, text_html = _body_parts(msg)
            out.append(FetchedEmail(
                uid=uid,
                message_id=_hdr(msg.get("Message-ID")).strip(),
                from_addr=parseaddr(_hdr(msg.get("From")))[1],
                to_addr=parseaddr(_hdr(msg.get("To")))[1],
                subject=_hdr(msg.get("Subject")),
                received_at=received or datetime.now(timezone.utc),
                text_body=text_plain,
                html_body=text_html,
                raw=raw_bytes.decode("utf-8", errors="replace"),
            ))
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def mark_seen(host: str, port: int, user: str, password: str, folder: str,
              uids: list[bytes]) -> None:
    """Set the \\Seen flag on the given UIDs so they aren't re-fetched next poll."""
    if not uids:
        return
    conn = imaplib.IMAP4_SSL(host, port)
    try:
        conn.login(user, password)
        conn.select(folder)
        conn.uid("STORE", b",".join(uids), "+FLAGS", "(\\Seen)")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
