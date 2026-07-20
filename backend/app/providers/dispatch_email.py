"""Parser for Dispatch (dispatch.me) job-notification emails — e.g. American Home Shield
work-order confirmations.

These are templated notification emails. Dispatch sends a `text/plain` alternative that
is a markdown/HTML hybrid with stably-labeled fields (`<strong>Label:</strong> value`), so
we parse that primarily and fall back to the `text/html` part if plain is missing.

Design per the agreed failure policy: extraction is *defensive* — every field is optional
and missing ones are simply absent. If a small set of REQUIRED fields can't be found we
mark the email `failed` (stored + flagged, never relayed) so a human can inspect it rather
than pushing half-parsed junk to GHL. When Dispatch changes the template this is where it
will break; keep it loud (parse_status='failed' + parse_error), never silent.
"""

import re
from dataclasses import dataclass, field

# Source key persisted on InboundEmail.source and the sender we filter/verify on.
SOURCE = "dispatch"
SENDER = "notifications@dispatch.me"

# Fields that must be present for an email to count as 'parsed' (and thus be relayed).
REQUIRED = ("job_id", "customer_name", "service_address")


@dataclass
class ParsedEmail:
    ok: bool
    fields: dict
    job_id: str | None = None
    error: str | None = None
    missing: list[str] = field(default_factory=list)


def matches(from_addr: str | None) -> bool:
    """True if this email is from the Dispatch sender we handle."""
    return bool(from_addr) and SENDER.lower() in from_addr.lower()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = _strip_tags(s)
    s = re.sub(r"\s+", " ", s).strip()
    # Trim leftover markdown / punctuation noise from the templated body.
    s = s.strip(" \t\r\n*:,")
    return s or None


def _labeled(text: str, label: str) -> str | None:
    """Extract the value after a `<strong>Label</strong>` (or `<strong>Label:</strong>`),
    tolerating the colon inside or outside the tag. Captures up to the next tag or newline."""
    pat = re.compile(
        r"<strong>\s*" + re.escape(label) + r"\s*:?\s*</strong>\s*:?\s*([^<\n]*)",
        re.IGNORECASE,
    )
    m = pat.search(text)
    return _clean(m.group(1)) if m else None


def _section(text: str, heading: str) -> str | None:
    """Return the raw block between an `<h1>/<h2>Heading</...>` and the next heading."""
    pat = re.compile(
        r"<h[12]>\s*" + re.escape(heading) + r"\s*</h[12]>(.*?)(?=<h[12]>|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    m = pat.search(text)
    return m.group(1) if m else None


def _parse_subject(subject: str | None) -> dict:
    """Subject e.g. 'American Home Shield Dispatch Email Confirmation: 66450639 ROOF Normal:NORMAL'
    → brand, job_id, service, priority."""
    out: dict = {}
    if not subject:
        return out
    subject = re.sub(r"\s+", " ", subject).strip()
    m = re.match(r"(.*?)\s+Dispatch E-?mail Confirmation", subject, re.IGNORECASE)
    if m:
        out["brand"] = m.group(1).strip()
    m = re.search(r"Confirmation:\s*(\d{4,})\s+(\S+)\s+(.+)$", subject, re.IGNORECASE)
    if m:
        out["job_id"] = m.group(1)
        out["service"] = m.group(2)
        out["priority"] = m.group(3).strip()
    return out


def _parse_contacts(text: str) -> list[dict]:
    """Customer Information block: pairs of `<strong>NAME</strong> (Role)` with the tel:
    link on the following line. Zipped in document order."""
    block = _section(text, "Customer Information") or ""
    names = re.findall(r"<strong>\s*([^<]+?)\s*</strong>\s*\(([^)]+)\)", block)
    phones = re.findall(r"tel:(\+?\d[\d\-\s()]*)", block)
    contacts = []
    for i, (name, role) in enumerate(names):
        phone = _normalize_phone(phones[i]) if i < len(phones) else None
        contacts.append({
            "name": _clean(name),
            "role": _clean(role),
            "phone": phone,
        })
    return contacts


def _normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if not raw.strip().startswith("+") else raw.strip()


def _parse_items(text: str) -> list[dict]:
    items = []
    for m in re.finditer(r"<h2>\s*Item\s*\d+:\s*(.*?)</h2>(.*?)(?=<h2>|<h1>|\Z)",
                         text, re.IGNORECASE | re.DOTALL):
        title = _clean(m.group(1))
        body = m.group(2)
        prob = re.search(r"<strong>\s*Problem:?\s*</strong>\s*(.*?)</p>", body,
                         re.IGNORECASE | re.DOTALL)
        items.append({
            "title": title,
            "problem": _clean(prob.group(1)) if prob else None,
            "status": _labeled(body, "Status"),
        })
    return items


def _parse_payment(text: str) -> dict:
    out = {}
    for key in ("Total", "Paid", "Remaining"):
        m = re.search(r"\*\*\s*" + key + r":\s*\$?\s*([\d,]+(?:\.\d{1,2})?)", text, re.IGNORECASE)
        if m:
            out[key.lower()] = m.group(1).replace(",", "")
    return out


def parse(subject: str | None, text_body: str | None, html_body: str | None) -> ParsedEmail:
    """Extract every field we can from a Dispatch email. `text_body` (the text/plain
    alternative) is preferred; `html_body` is the fallback if plain is empty."""
    body = text_body or html_body or ""

    f: dict = {"source": SOURCE}
    f.update(_parse_subject(subject))

    # Contacts + customer. The "Contract Contact" is the property owner / AHS member;
    # prefer it for the primary customer, else the first contact.
    contacts = _parse_contacts(body)
    if contacts:
        f["contacts"] = contacts
        primary = next((c for c in contacts if "contract" in (c.get("role") or "").lower()),
                       contacts[0])
        if primary.get("name"):
            # Template names are SHOUTING; title-case for a clean GHL contact.
            f["customer_name"] = primary["name"].title()
        if primary.get("phone"):
            f["customer_phone"] = primary["phone"]

    # Customer email — first address in the body that isn't a dispatch.me / template address.
    for addr in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", body):
        low = addr.lower()
        if "dispatch.me" in low or "sendgrid" in low:
            continue
        f["customer_email"] = addr
        break

    # A phone from the top summary block, if the contact block didn't yield one.
    if "customer_phone" not in f:
        m = re.search(r"\+\d{10,15}", body)
        if m:
            f["customer_phone"] = m.group(0)

    # Service / covered-property address.
    addr_block = _section(body, "Covered Property Address")
    if addr_block:
        f["service_address"] = _clean(addr_block)

    # Work-order + contract + vendor fields.
    f["priority"] = _labeled(body, "Dispatch Priority") or f.get("priority")
    f["autho_required"] = _labeled(body, "Autho Required?")
    f["vendor_id"] = _labeled(body, "Vendor ID")
    f["contract_id"] = _labeled(body, "Contract ID")
    for label, key in (
        ("Listing Effective Date", "listing_effective_date"),
        ("Listing Expiration Date", "listing_expiration_date"),
        ("Contract Effective Date", "contract_effective_date"),
        ("Contract Expiration Date", "contract_expiration_date"),
    ):
        val = _labeled(body, label)
        if val:
            f[key] = val

    items = _parse_items(body)
    if items:
        f["items"] = items

    payment = _parse_payment(body)
    if payment:
        f["payment"] = payment

    # Coverage notes: each <p> under the Coverage Notes heading.
    notes_block = _section(body, "Coverage Notes")
    if notes_block:
        notes = [_clean(p) for p in re.findall(r"<p>(.*?)</p>", notes_block, re.DOTALL)]
        notes = [n for n in notes if n]
        if notes:
            f["coverage_notes"] = notes

    # job_id: subject is most reliable; fall back to the body summary line.
    if not f.get("job_id"):
        m = re.search(r"(?m)^\s*(\d{5,})\s+[A-Z]", body)
        if m:
            f["job_id"] = m.group(1)

    # Drop keys that resolved to None so `fields` stays clean.
    f = {k: v for k, v in f.items() if v is not None}

    missing = [k for k in REQUIRED if not f.get(k)]
    if missing:
        return ParsedEmail(
            ok=False, fields=f, job_id=f.get("job_id"),
            error="missing required fields: " + ", ".join(missing), missing=missing,
        )
    return ParsedEmail(ok=True, fields=f, job_id=f.get("job_id"))
