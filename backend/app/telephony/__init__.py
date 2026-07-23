"""In-platform calling (Ticket 13) — WebRTC softphone leg.

Pure, dependency-light helpers for the browser softphone seam:
- `credentials.py`: mint short-lived SIP + coturn (TURN) credentials at app-login time.
- `control.py`: the ARI control ORCHESTRATION (bridge / hold / blind-transfer) the backend
  drives server-side over an injected `AriControlOps`.

Both modules import ONLY stdlib (no fastapi/httpx/sqlalchemy/pydantic) so they stay unit-
testable in the sandbox with fakes — the FastAPI endpoints in app/api/telephony.py and the
concrete AsteriskAriClient (httpx) are thin wrappers over these.
"""
