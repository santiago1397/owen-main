"""Unit test for the Dispatch/AHS email parser + the IMAP body extraction.

Built against a real American Home Shield dispatch confirmation. No network, no DB — pure
parsing. Exercises: subject parse, contact/customer extraction, address, work-order/contract
fields, items, payment, coverage notes; the quoted-printable decode path in mailbox; and the
fail-and-flag policy when required fields are missing.

Run: python -m tests.test_dispatch_email
"""

import email
import sys

from app.providers import dispatch_email
from app.services import mailbox

SUBJECT = "American Home Shield Dispatch Email Confirmation: 66450639 ROOF Normal:NORMAL"

# The text/plain alternative, as it looks AFTER quoted-printable decoding (what the parser
# sees). Trimmed to the field-bearing body; layout matches the real email.
PLAIN_BODY = """\
****************************
Dispatch E-mail Confirmation
****************************

You've received a new dispatch from American Home Shield.

**
66450639 ROOF Normal:NORMAL

Guillermo Escala 14436 SW 95TH LN
MIAMI, FL 33186 +13059629757
scalas02@yahoo.com

<h1>Job Brand Information</h1>

<p>This is an AHS customer.</p>

<h1>Customer Information</h1>

<p><strong>IVA ESCALA</strong> (Dispatch Contact)</p>

<p><strong>HOME:</strong><a href="tel:+13059686235">(305) 968-6235</a></p>

<p><strong>GUILLERMO ESCALA</strong> (Contract Contact)</p>

<p><strong>Home:</strong><a href="tel:+13059629757">(305) 962-9757</a></p>

<h1>Vendor</h1>

<p><strong>Vendor ID</strong>:677998</p>

<h1>Contract Information</h1>

<p><strong>Contract ID:</strong>11007009</p>

<p><strong>Contract Effective Date:</strong> 2026-06-29</p>

<p><strong>Contract Expiration Date:</strong> 2027-06-29</p>

<h1>Covered Property Address</h1>

<p>14436 SW 95TH LN
MIAMI, FL 33186</p>

<h1>Work Order Information</h1>

<p><strong>Dispatch Priority:</strong>Normal</p>

<p><strong>Autho Required?:</strong> False</p>

<h2>Item 1: Roof Leaks</h2>

<p><strong>Problem:</strong>
Leaking, OTHER,</p>

<p><strong>Status:</strong> Open</p>

<h1>Coverage Information</h1>

<h2>Coverage Notes</h2>

<p>SHIELDPLATINUM HOME WARRANTY</p>

<p>*** Payment Type : PREPAY ***</p>

<h2>Coverage Details</h2>

<p>**Total: $125</p>

<p>**Paid: $0</p>

<p>**Remaining: $125</p>
"""


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"dispatch email parse failed at: {name}")


def main():
    print("matches — sender filter:")
    check("dispatch sender matches", dispatch_email.matches("Dispatch <notifications@dispatch.me>"))
    check("other sender rejected", not dispatch_email.matches("spam@evil.com"))

    print("parse — full extraction from the real sample:")
    parsed = dispatch_email.parse(SUBJECT, PLAIN_BODY, "")
    f = parsed.fields
    check("ok (all required present)", parsed.ok)
    check("job_id from subject", f["job_id"] == "66450639")
    check("service from subject", f["service"] == "ROOF")
    check("brand from subject", f["brand"] == "American Home Shield")
    check("customer_name = Contract Contact, title-cased", f["customer_name"] == "Guillermo Escala")
    check("customer_phone normalized", f["customer_phone"] == "+13059629757")
    check("customer_email extracted", f["customer_email"] == "scalas02@yahoo.com")
    check("service_address collapsed", f["service_address"] == "14436 SW 95TH LN MIAMI, FL 33186")
    check("vendor_id", f["vendor_id"] == "677998")
    check("contract_id", f["contract_id"] == "11007009")
    check("contract effective date", f["contract_effective_date"] == "2026-06-29")
    check("priority (work order)", f["priority"] == "Normal")
    check("autho_required", f["autho_required"] == "False")
    check("two contacts parsed", len(f["contacts"]) == 2)
    check("dispatch contact phone", f["contacts"][0]["phone"] == "+13059686235")
    check("item title", f["items"][0]["title"] == "Roof Leaks")
    check("item problem", f["items"][0]["problem"] == "Leaking, OTHER")
    check("item status", f["items"][0]["status"] == "Open")
    check("payment total", f["payment"]["total"] == "125")
    check("payment remaining", f["payment"]["remaining"] == "125")
    check("coverage notes captured", "SHIELDPLATINUM HOME WARRANTY" in f["coverage_notes"])

    print("mailbox._body_parts — quoted-printable decode path:")
    # A raw wire-format email with a QP soft line break (=\n) and an encoded '=' (=3D),
    # exactly as Dispatch sends it — _body_parts must decode both.
    raw = (
        b"From: notifications@dispatch.me\r\n"
        b"Subject: test\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=us-ascii\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"66450639 continues=\r\nhere href=3D\"tel:+13059629757\"\r\n"
    )
    msg = email.message_from_bytes(raw)
    text_plain, _ = mailbox._body_parts(msg)
    check("QP soft-break joined", "continueshere" in text_plain.replace(" ", ""))
    check("QP =3D decoded to =", 'href="tel:' in text_plain)

    print("parse — fail-and-flag when required fields missing:")
    bad = dispatch_email.parse("Random subject with no job info", "just some text", "")
    check("not ok", not bad.ok)
    check("error lists missing fields", "missing required fields" in (bad.error or ""))
    check("job_id in missing", "job_id" in bad.missing)

    print("\nALL DISPATCH EMAIL CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
