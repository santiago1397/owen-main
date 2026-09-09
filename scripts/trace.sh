#!/usr/bin/env bash
# End-to-end trace of ONE call, merged across every place a call leaves evidence.
#
# WHY: a call crosses four processes — the carrier's SIP into native Asterisk, the worker's ARI
# consumer + flow interpreter, owen-voice for the agent audio, and the app's DB projection.
# Each logs somewhere different, so answering "where did it fail?" meant four manual greps with
# four different timestamp formats, and the most important source (Asterisk) logged nothing at
# all until asterisk/logger.conf was added. This prints one timeline, in order, from all of them.
#
# Run ON the VPS:
#     bash scripts/trace.sh last              # the most recent Asterisk call
#     bash scripts/trace.sh 1788978501.86     # a specific linkedid
#     bash scripts/trace.sh --arrivals        # every INVITE seen today, answered or not
#
# The `--arrivals` mode is the one that answers "did the call even reach us", which is a
# different question from "what did the flow do" and was the harder one to answer.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"
AST_LOG=/var/log/asterisk/owen-calls.log
SINCE="${TRACE_SINCE:-6h}"

c_hdr=$'\033[1;36m'; c_dim=$'\033[2m'; c_warn=$'\033[1;33m'; c_off=$'\033[0m'
hdr() { printf "\n${c_hdr}%s${c_off}\n" "$1"; }

# --- arrivals: did anything reach the box at all? -------------------------------------------
if [ "${1:-}" = "--arrivals" ]; then
  # THE authoritative source, and the one I overlooked for an hour: cdr.conf sets
  # `unanswered=yes`, so Asterisk writes a row for EVERY call attempt -- including one that
  # rang and never connected. If a call is absent here, it did not reach the box. No log
  # parsing, no verbosity settings, no guessing.
  hdr "=== 0. ASTERISK CDR — every attempt, answered or not (authoritative) ==="
  $COMPOSE exec -T app python - <<'PY' 2>/dev/null || echo "  (query failed)"
import asyncio
from sqlalchemy import text
from app.db import SessionLocal
async def main():
    async with SessionLocal() as db:
        r = await db.execute(text(
            "select start, src, dst, dcontext, lastapp, disposition, duration, billsec, "
            "uniqueid from cdr where start > now() - interval '24 hours' order by start desc"))
        rows = r.fetchall()
        if not rows:
            print("  NO call attempts in 24h — nothing reached Asterisk at all")
        for x in rows:
            print(f"  {str(x[0])[:19]}  {str(x[1] or '?'):16} -> {str(x[2] or '?'):14} "
                  f"{str(x[5]):10} dur={x[6]}s bill={x[7]}s  uid={x[8]}")
asyncio.run(main())
PY

  hdr "=== 1. SIP-level events (rejections, auth failures) ==="
  if [ -r "$AST_LOG" ]; then
    sudo grep -aE "Call from|INVITE|Rejected|failed to authenticate|No matching endpoint" "$AST_LOG" 2>/dev/null | tail -40 \
      || echo "  (none)"
  else
    echo "  ${AST_LOG} not readable — is asterisk/logger.conf deployed and 'logger reload' run?"
  fi
  hdr "=== channels Asterisk has processed since start ==="
  sudo asterisk -rx "core show channels" 2>/dev/null | tail -3
  hdr "=== calls BulkVS has rated in the last 24h ==="
  $COMPOSE exec -T app python - <<'PY' 2>/dev/null || echo "  (query failed)"
import asyncio, time
from app.providers.bulkvs_client import fetch_voice_cdr
async def main():
    now = int(time.time())
    try:
        recs = await fetch_voice_cdr(now - 86400, now, "all")
    except Exception as e:
        print("  BulkVS /voice failed:", e); return
    if not recs:
        print("  no rated records — nothing reached the carrier, or nothing connected")
    for r in sorted(recs, key=lambda x: str(x.get("callStart"))):
        print(f"  {r.get('callStart')}  {r.get('callSource')} -> {r.get('callDestination')}"
              f"  {r.get('durationSecs')}s  ${r.get('amount')}")
asyncio.run(main())
PY
  exit 0
fi

LID="${1:-last}"
if [ "$LID" = "last" ]; then
  LID=$($COMPOSE exec -T app python - <<'PY' 2>/dev/null | tr -d '\r'
import asyncio
from sqlalchemy import select, desc
from app.db import SessionLocal
from app.models import Call
async def main():
    async with SessionLocal() as db:
        # Asterisk linkedids look like 1788978501.86; carrier SIDs (CA.../ b...) are other
        # providers and have no Asterisk-side story to tell.
        rows = (await db.execute(
            select(Call).where(Call.started_at.isnot(None))
            .order_by(desc(Call.started_at)).limit(40))).scalars().all()
        for c in rows:
            sid = str(c.provider_call_sid or "")
            if sid and sid[0].isdigit() and "." in sid:
                print(sid); return
asyncio.run(main())
PY
)
  [ -z "$LID" ] && { echo "no recent Asterisk call found"; exit 1; }
fi

printf "${c_hdr}TRACE %s${c_off}\n" "$LID"

hdr "=== 1. CARRIER -> ASTERISK (SIP) ==="
if [ -r "$AST_LOG" ]; then
  sudo grep -a "$LID" "$AST_LOG" 2>/dev/null | head -60 || echo "  (nothing for this linkedid)"
else
  echo "  ${c_warn}${AST_LOG} missing — deploy asterisk/logger.conf and run 'asterisk -rx \"logger reload\"'${c_off}"
fi

hdr "=== 2. WORKER: ARI consumer + flow interpreter ==="
docker logs callmon_worker --since "$SINCE" 2>&1 | grep -a "$LID" | tail -40 || echo "  (none)"

hdr "=== 3. OWEN-VOICE: agent media session ==="
docker logs owen_voice --since "$SINCE" 2>&1 | grep -a "$LID" | tail -40 || echo "  (none)"
# The session uuid is minted per call; once known, its own lines carry it rather than the lid.
SESS=$(docker logs owen_voice --since "$SINCE" 2>&1 | grep -a "$LID" \
        | grep -oE "session [0-9a-f-]{36}" | head -1 | awk '{print $2}')
if [ -n "$SESS" ]; then
  echo "  --- session $SESS ---"
  docker logs owen_voice --since "$SINCE" 2>&1 | grep -a "$SESS" \
    | grep -avE "GET /health|asterisk/info" | tail -40
fi

hdr "=== 4. DB: call row, flow events, captured trace lines ==="
$COMPOSE exec -T app python - "$LID" <<'PY' 2>/dev/null || echo "  (query failed)"
import asyncio, json, sys
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Call, CallEvent, Number, AppLog
lid = sys.argv[1]
async def main():
    async with SessionLocal() as db:
        c = (await db.execute(select(Call).where(Call.provider_call_sid == lid))).scalars().first()
        if not c:
            print("  no `calls` row — the flow never ran, or ingestion never saw it")
        else:
            n = (await db.execute(select(Number).where(Number.id == c.number_id))).scalars().first()
            print(f"  call to {n.phone_number if n else '?'}  status={c.status} "
                  f"started={str(c.started_at)[:19]} ended={str(c.ended_at)[:19]} "
                  f"flow_version={str(c.flow_version_id)[:8]}")
            ev = (await db.execute(select(CallEvent).where(CallEvent.call_id == c.id))).scalars().all()
            def key(e):
                f = (e.payload or {}).get("flow") or {}
                return f.get("step", 0)
            for e in sorted(ev, key=key):
                f = (e.payload or {}).get("flow") or {}
                bits = {k: v for k, v in f.items()
                        if k in ("node_id", "node_type", "port", "routed", "ms",
                                 "agent_port", "agent_turns", "ended", "path")}
                print(f"    {e.event_type:22} {json.dumps(bits)}")
        try:
            logs = (await db.execute(
                select(AppLog).where(AppLog.linkedid == lid).order_by(AppLog.at))).scalars().all()
            print(f"  --- {len(logs)} captured log lines (app_logs) ---")
            for l in logs:
                print(f"    {str(l.at)[:19]} {l.service:7} {l.level:7} {l.message[:120]}")
        except Exception as e:
            print("  app_logs query failed:", e)
asyncio.run(main())
PY

hdr "=== VERDICT HINTS ==="
cat <<'EOF'
  Start with `--arrivals`. Its CDR section is authoritative because cdr.conf sets
  unanswered=yes: an attempt that rang and failed is still a row. No row = it never got here.

  no CDR row       -> the INVITE never reached Asterisk. The fault is upstream: carrier
                      routing, or the number is not actually being delivered to this trunk.
                      Nothing on this box can fix it; take the TN to the carrier.
  CDR + a REJECT   -> we refused it (identify/auth). Compare pjsip.conf match IPs against the
                      source IP in section 1.
  CDR, 2 empty     -> Asterisk took it but Stasis never fired: ARI consumer down, or the call
                      landed in a context that does not Stasis() into ARI_APP.
  2 ok, 3 empty    -> the flow ran but never reached an ai_agent node (or owen-voice was down).
  3 ok, 4 empty    -> audio ran but nothing projected: ingestion problem, not a call problem.
EOF
