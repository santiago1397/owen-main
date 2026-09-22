"""The body of a `crm_report` job, built where it can be tested.

This used to be inline in `flows/runtime.py::_enqueue_crm_report`, which imports httpx and
sqlalchemy and so cannot be imported by the dependency-free tests in this repo. Two fields
were wrong for as long as it lived there, and neither raised:

  * **`transfer` was ALWAYS null.** The report was queued *before* the transfer was
    resolved, and the destination is only added to `data` afterwards
    (`runtime.py`: `return ("transferred", {**data, "transfer": chosen})`). So the CRM was
    told a call ended on port `transfer` and never where it went — for a supervised agent,
    "it handed the customer to someone" with no way to say who.
  * **`duration_s` was hardcoded `None`.** owen-voice knows the figure (`MediaSession.
    duration_s`); it simply was not carried back. A CRM timeline entry that cannot say how
    long the call lasted cannot answer "is the agent keeping people on the phone?", which
    is the first question asked of any agent in its supervised phase.

Both are fixed at the source: the caller resolves the transfer FIRST and passes the final
port and data here, and `duration_s` now rides back on the session result.

Nothing in this module does I/O, and nothing imported here does either — that is the point.
"""
from app.agents.capture import normalise_capture

# Ports a session can finish on, plus the one the flow runtime substitutes once it has
# actually moved the caller. Kept here so a typo in a port name is a failing test rather
# than a CRM timeline entry saying something nobody will read closely.
KNOWN_OUTCOMES = frozenset({"transfer", "transferred", "end_call", "default", "failed"})


def build_report_body(*, linkedid: str, caller_number: str | None, port: str,
                      data: dict | None, owen_url: str | None = None) -> dict:
    """The JSON body of a `crm_report` job.

    `port` is the FINAL outcome — `transferred` rather than `transfer` once the caller has
    actually been moved — and `data` is the result dict as it stands at that point, so a
    resolved destination is present to be reported.
    """
    data = data or {}

    captures: list[dict] = []
    captured = data.get("captured")
    if isinstance(captured, dict) and captured:
        fields = normalise_capture(captured)
        if fields:
            captures.append({"fields": fields})

    # The destination the agent picked, once it has been moved there. A dict from the
    # agent's own allowlist (`{"name": ..., "kind": ..., ...}`); anything else is ignored
    # rather than guessed at.
    transfer = data.get("transfer")
    transfer_name = transfer.get("name") if isinstance(transfer, dict) else None

    return {
        "linkedid": linkedid,
        "caller_number": caller_number or "",
        "outcome": port,
        # Seconds, as owen-voice measured them. None when it did not report one — an
        # unknown duration is unknown, never 0, which would read as an instant hang-up.
        "duration_s": _duration(data),
        "captures": captures,
        "transfer": transfer_name,
        "owen_url": owen_url or None,
    }


def _duration(data: dict) -> float | None:
    """Seconds from the session result, or None.

    Read from `duration_s` first, then from the metrics blob, because the two travel
    separately and a future change to either should not silently drop the figure.
    """
    for source in (data, data.get("metrics") if isinstance(data.get("metrics"), dict) else {}):
        raw = (source or {}).get("duration_s")
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return round(value, 2)
    return None
