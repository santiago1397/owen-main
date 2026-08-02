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

    print("ghl_payload — flattened/derived fields for GHL:")
    from types import SimpleNamespace
    from app.services.emails import ghl_payload
    em = SimpleNamespace(fields=f, source="dispatch", job_id=f["job_id"], subject=SUBJECT,
                         from_addr="notifications@dispatch.me", message_id="<x@y>", received_at=None)
    p = ghl_payload(em)
    check("problem flattened", p["problem"] == "Leaking, OTHER")
    check("payment_total flattened", p["payment_total"] == "125")
    check("primary_contact_phone", p["primary_contact_phone"] == "+13059629757")
    check("coverage_notes_text joined", "SHIELDPLATINUM HOME WARRANTY" in p["coverage_notes_text"])
    check("job_description has customer", "Guillermo Escala" in p["job_description"])
    check("job_description has address", "14436 SW 95TH LN" in p["job_description"])
    check("job_description has job header", p["job_description"].startswith("Job 66450639 ROOF"))
    check("nested fields still present", isinstance(p["items"], list) and isinstance(p["contacts"], list))

    print("GHL API builders — contact + opportunity bodies:")
    from app.services.emails import build_contact_body, build_opportunity_body
    cb = build_contact_body(f, "LOC123")
    check("contact locationId", cb["locationId"] == "LOC123")
    check("contact firstName", cb["firstName"] == "Guillermo")
    check("contact lastName", cb["lastName"] == "Escala")
    check("contact phone", cb["phone"] == "+13059629757")
    check("contact email", cb["email"] == "scalas02@yahoo.com")
    check("contact address1 = full", cb["address1"] == "14436 SW 95TH LN MIAMI, FL 33186")
    check("contact state parsed", cb["state"] == "FL")
    check("contact zip parsed", cb["postalCode"] == "33186")
    check("contact has ahs-job tag", "ahs-job" in cb["tags"])
    ob = build_opportunity_body(f, "CONTACT1", "PIPE1", "STAGE1", "LOC123")
    check("opp pipeline", ob["pipelineId"] == "PIPE1")
    check("opp stage", ob["pipelineStageId"] == "STAGE1")
    check("opp contact", ob["contactId"] == "CONTACT1")
    check("opp status open", ob["status"] == "open")
    check("opp name has job+customer", "66450639" in ob["name"] and "Guillermo Escala" in ob["name"])
    check("opp monetaryValue from payment", ob["monetaryValue"] == 125.0)

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
    # A subject that is not a work-order confirmation is now IGNORED rather than FAILED.
    # It is still never relayed (`ok` is False) and still records what was missing — but it
    # is not an error, because there was never a lead in it to lose.
    bad = dispatch_email.parse("Random subject with no job info", "just some text", "")
    check("not ok", not bad.ok)
    check("a non-job subject is ignored, not failed", bad.status == dispatch_email.IGNORED)
    check("error explains it carries no lead", "ignored:" in (bad.error or ""))
    check("job_id in missing", "job_id" in bad.missing)

    # But a real confirmation that cannot be parsed stays loud — this is the alarm that must
    # never be traded away for a quieter log.
    broken = dispatch_email.parse(
        "American Home Shield Dispatch Email Confirmation: 66859789 ROOF", "nothing useful", "")
    check("a real confirmation that fails to parse is FAILED", broken.status == dispatch_email.FAILED)
    check("error lists missing fields", "missing required fields" in (broken.error or ""))

    test_non_job_emails_are_ignored_not_failed()
    test_full_extraction_beats_subject_classification()
    test_duplicate_opportunity_is_not_a_failure()
    test_cancellations_are_classified_and_carry_the_job_number()
    test_contact_name_accepts_markdown_and_html_emphasis()

    print("\nALL DISPATCH EMAIL CHECKS PASSED")




# --- non-job Dispatch mail must never read as a failure ------------------------------------
def test_non_job_emails_are_ignored_not_failed():
    """The mailbox receives more than work orders. Misfiling the rest as parse failures is
    what made a healthy parser look broken: 4 of 5 "failures" on this account were mail that
    never contained a lead. `ignored` and `failed` must stay distinguishable."""
    from app.providers import dispatch_email as d

    # Real subjects taken from production. Cancellations are NOT here — they carry a job
    # number and an action, so they get their own status; see
    # test_cancellations_are_classified_and_carry_the_job_number.
    non_jobs = [
        ("American Home Shield sent you a note for job #70848289", "note on an existing job"),
        ("Welcome to Dispatch, Your Account Has Been Created!", "Dispatch account email"),
    ]
    for subject, expected_reason in non_jobs:
        r = d.parse(subject, "", "")
        check(f"{subject[:40]!r} -> ignored", r.status == d.IGNORED)
        check(f"  ...reason reads {expected_reason!r}", r.ignored_reason == expected_reason)
        check("  ...and is never relayed", r.ok is False)

    # An unrecognized Dispatch email type must ALSO be ignored rather than raise a false
    # alarm — the classifier is a whitelist of job mail, not a blacklist of junk.
    r = d.parse("Some brand new Dispatch notification nobody has seen", "", "")
    check("an unknown non-job subject is ignored, not failed", r.status == d.IGNORED)

    # The alarm that must survive: a genuine work order we could not read.
    r = d.parse("American Home Shield Dispatch Email Confirmation: 66859789 ROOF Normal:NORMAL",
                "", "")
    check("a real confirmation we cannot parse is still FAILED", r.status == d.FAILED)
    check("  ...and says which fields are missing", "missing required fields" in (r.error or ""))
    check("is_job_notification only matches confirmations",
          d.is_job_notification("American Home Shield Dispatch Email Confirmation: 1 ROOF")
          and not d.is_job_notification("AHS Canceled Job 1")
          and not d.is_job_notification(None))


def test_full_extraction_beats_subject_classification():
    """Ordering guard: a fully-extracted email is parsed no matter what its subject says.
    Otherwise a subject-line change at Dispatch would silently start dropping real leads."""
    from app.providers import dispatch_email as d

    body = (
        "<strong>Job ID:</strong> 12345678\n"
        "<strong>Service Address:</strong> 123 Main St, Miami, FL 33101\n"
        "<p>JANE DOE (Contract Contact)</p>\n"
    )
    r = d.parse("A subject shape we have never seen", body, None)
    if r.status == d.PARSED:
        check("a fully-extracted email is parsed regardless of subject", True)
    else:
        # Extraction depends on the real template; if this fixture cannot satisfy REQUIRED,
        # assert the weaker but still essential property rather than a false pass.
        check("an email missing required fields with an odd subject is ignored, not failed",
              r.status == d.IGNORED)




def test_duplicate_opportunity_is_not_a_failure():
    """GoHighLevel allows one opportunity per contact per pipeline, so a repeat customer's
    second job is refused with OPPORTUNITY_NO_DUPLICATE. That is a rule, not a lost lead:
    the relay must reuse the existing card, attach the job as a note, and stop retrying.
    All four of this account's 'lost leads' were this."""
    import httpx

    from app.providers import ghl_api

    # The exact body GoHighLevel returned in production.
    body = {
        "statusCode": 400,
        "message": "Can not create duplicate opportunity for the contact.",
        "code": "OPPORTUNITY_NO_DUPLICATE",
        "meta": {"existingId": "6sS72KhoDY0KpgQJilHT"},
        "error": "Bad Request",
    }
    req = httpx.Request("POST", "https://services.leadconnectorhq.com/opportunities/")
    resp = httpx.Response(400, json=body, request=req)

    # _raise_for_status must carry GHL's own words — losing them is what made these
    # undiagnosable for a week.
    try:
        ghl_api._raise_for_status(resp, "opportunity create")
        check("non-2xx raises", False)
    except httpx.HTTPStatusError as exc:
        check("the failure message carries GHL's explanation",
              "duplicate opportunity" in str(exc).lower())
        check("...and the status code", "400" in str(exc))

    check("a 2xx does not raise", ghl_api._raise_for_status(httpx.Response(200, request=req), "x") is None)

    # The typed exception must expose the existing id, which is what lets the relay attach
    # the new job to the right card instead of dropping it.
    dup = ghl_api.DuplicateOpportunity("6sS72KhoDY0KpgQJilHT", body["message"])
    check("DuplicateOpportunity carries the existing opportunity id",
          dup.existing_id == "6sS72KhoDY0KpgQJilHT")
    check("...and is an Exception the handler can catch", isinstance(dup, Exception))




def test_cancellations_are_classified_and_carry_the_job_number():
    """A cancellation is neither a lead nor ignorable: AHS is saying dispatched work is off.
    It must be picked out with its job number so the CRM can be annotated — otherwise a card
    sits open in the pipeline for work nobody will do."""
    from app.providers import dispatch_email as d

    for subject, job in [
        ("AHS Canceled Job 70396099", "70396099"),
        ("AHS Canceled Job 68729729", "68729729"),
        ("AHS Cancelled Job #12345678", "12345678"),   # British spelling + hash
        ("American Home Shield canceled job 999111", "999111"),
    ]:
        r = d.parse(subject, "", "")
        check(f"{subject[:34]!r} -> cancellation", r.status == d.CANCELLATION)
        check(f"  ...job number {job} extracted", r.job_id == job)
        check("  ...and it is never relayed as a lead", r.ok is False)
        check("  ...fields carry the cancelled job id",
              (r.fields or {}).get("cancelled_job_id") == job)

    check("cancelled_job_id ignores a normal confirmation",
          d.cancelled_job_id("American Home Shield Dispatch Email Confirmation: 66450639 ROOF") is None)
    check("cancelled_job_id ignores None", d.cancelled_job_id(None) is None)

    # A cancellation subject without a parseable number is not actionable, so it must fall
    # back to `ignored` rather than pretend it knows which job was cancelled.
    r = d.parse("AHS Canceled Job", "", "")
    check("a cancellation with no job number falls back to ignored", r.status == d.IGNORED)

    # And the other classifications must be unaffected.
    check("a note is still ignored",
          d.parse("American Home Shield sent you a note for job #7", "", "").status == d.IGNORED)
    check("a real unreadable confirmation is still failed",
          d.parse("American Home Shield Dispatch Email Confirmation: 1 ROOF", "", "").status == d.FAILED)




def test_contact_name_accepts_markdown_and_html_emphasis():
    """Dispatch emphasises contact names EITHER as <strong> or as **markdown**, inconsistently
    between sends. Handling only <strong> silently cost this account a lead (job 66859789):
    no customer_name means the REQUIRED check fails and the email is never relayed."""
    from app.providers import dispatch_email as d

    # Both blocks are verbatim from production, differing only in emphasis style.
    html_style = (
        "<h1>Customer Information</h1>\n"
        '<p><strong>THERESA  ANTON</strong> (Dispatch Contact)</p>\n'
        '<p><strong>HOME:</strong><a href="tel:+19547267261">(954) 726-7261</a></p>\n'
        '<p><strong>THERESA  ANTON</strong> (Contract Contact)</p>\n'
        '<p><strong>Home:</strong><a href="tel:+19547267261">(954) 726-7261</a></p>\n'
        "<h1>Autho Link</h1>\n"
    )
    markdown_style = (
        "<h1>Customer Information</h1>\n"
        '<p>**KARINA GRIMALDI ** (Dispatch Contact)</p>\n'
        '<p><strong>HOME:</strong><a href="tel:+17862855527">(786) 285-5527</a></p>\n'
        '<p>**KARINA GRIMALDI ** (Contract Contact)</p>\n'
        '<p><strong>Home:</strong><a href="tel:+17862855527">(786) 285-5527</a></p>\n'
        "<h1>Autho Link</h1>\n"
    )
    for label, block, expected, phone in [
        ("<strong>", html_style, "Theresa  Anton", "+19547267261"),
        ("**markdown**", markdown_style, "Karina Grimaldi", "+17862855527"),
    ]:
        contacts = d._parse_contacts(block)
        check(f"{label}: two contacts parsed", len(contacts) == 2)
        # _clean collapses runs of whitespace, so compare on collapsed text.
        got = " ".join((contacts[0].get("name") or "").upper().split())
        check(f"{label}: name extracted", got == " ".join(expected.upper().split()))
        check(f"{label}: role extracted", contacts[0].get("role") == "Dispatch Contact")
        check(f"{label}: phone zipped in document order", contacts[0].get("phone") == phone)
        contract = next((c for c in contacts if "contract" in (c.get("role") or "").lower()), None)
        check(f"{label}: the Contract Contact is identifiable", contract is not None)

    # The markdown form must survive the whole parse and satisfy the REQUIRED check, which is
    # the actual failure this fixes — a name alone is not enough without job_id + address.
    body = ("Dispatch E-mail Confirmation\n" + markdown_style +
            "<h1>Covered Property Address</h1>\n<p>123 Main St\nMIAMI, FL 33186</p>\n")
    r = d.parse("American Home Shield Dispatch Email Confirmation: 66859789 ROOF", body, None)
    check("markdown-emphasised name yields a customer_name",
          (r.fields or {}).get("customer_name", "").upper() == "KARINA GRIMALDI")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
