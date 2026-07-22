# Telephony infra — security + deployment of native Asterisk

Type: grilling
Status: resolved (2026-07-22 — grilling; additive, no regression to Twilio/SignalWire/GHL paths)
Assignee: svillahermosa
Claimed by: wayfinder session 2026-07-22 (ticket 09)
Blocked by: 01, 02

## Question

How does native Asterisk live safely alongside the Traefik/Docker + native-Postgres stack?

- **SIP security:** IP-ACL the SIP/RTP ports to BulkVS ranges only (from ticket 01); no open registration;
  fail2ban; TLS/SRTP if BulkVS supports it. RTP port range + firewall/NAT on the VPS.
- **ARI exposure:** ARI bound to localhost only; credentials management; how app/worker containers reach it
  (`host.docker.internal` + `extra_hosts`), matching the existing Postgres-on-host pattern.
- **Deploy/ops:** Asterisk stays native (not containerised — media wants the host). How is its config
  version-controlled and deployed vs. the `make deploy` Docker flow? Restart/upgrade story, `/health` for the
  telephony path, monitoring (registered trunk, active channels).
- **Coexistence:** confirm nothing here disturbs the live Twilio path.

Use `/grilling`; reconcile with `../../santiago/SERVER_SETUP.md` convention.

## Answer

**Resolved 2026-07-22 via grilling.** Native Asterisk lives alongside the Traefik/Docker + native-Postgres
stack by **reusing the existing "native host service + per-project bridge-subnet allowlist" mold** (the same
one Postgres already uses), and by keeping every new surface **additive, flag-gated, and reversible**. The
existing Twilio / SignalWire / GHL paths are untouched. Grounded in the real ticket-04 spike (Asterisk 22.10.1,
ARI `127.0.0.1:8088` user `owen`, RTP from `152.188.166.x`, SIP UFW-locked to the 4 BulkVS SBC IPs).

### 1. Config version-control & deploy (native, but in git)
- Asterisk stays **native on the host** (media wants the host — already decided), but the config files **we own**
  come **into this repo under a top-level `asterisk/` dir** mirroring the relevant `/etc/asterisk/*.conf`
  (`pjsip.conf`, `extensions.conf`/dialplan, `http.conf`, `ari.conf`, `rtp.conf`). We manage **only the files we
  own** — Asterisk's stock includes are left alone.
- Deploy stays git-based: extend `scripts/deploy.sh` with an Asterisk step that, after `git pull`, **`rsync`s
  `asterisk/*.conf` → `/etc/asterisk/`** and issues a **targeted reload** (`asterisk -rx "pjsip reload"`,
  `"dialplan reload"`, …) — **never a restart**, so live calls survive config changes.
- **Secrets stay out of git** — ARI password / any creds are rendered on deploy from `.env.prod` (or referenced
  from `/root/.owen-*`), exactly how Postgres creds already work.

### 2. SIP/RTP firewall — asymmetric (strict signaling, session-validated media)
- **SIP `5060/udp`:** keep the hard UFW IP-allowlist to exactly the 4 BulkVS SBC IPs (`162.249.171.198`,
  `76.8.29.198`, `69.12.88.198`, `199.255.157.198`). This is the real attack surface (live SIP brute-force was
  already caught here); it stays sealed. `chan_pjsip` = IP-auth `identify` only, **no open registration**.
- **RTP media:** narrow the RTP range in `rtp.conf` to **`10000–10200/udp`** (~100 concurrent calls, far beyond a
  single-org tool's need) and open **only that range** in UFW. It is **not** IP-restricted — RTP arrives from
  `152.188.166.x`, a range that can't be reliably enumerated — the real filter is **Asterisk dropping any RTP with
  no matching active negotiated session** (an open RTP port with no session is inert).
- **fail2ban** stays on the Asterisk security log for SIP.

### 3. ARI exposure — reuse the Postgres-on-host model
- **Pin `callmon-net` to a fixed subnet** in `docker-compose.prod.yml` (`ipam.config.subnet`, e.g.
  `172.28.0.0/16`) so firewall/allowlist targets are **stable** (today it's Docker-auto-assigned and can drift).
  **Apply the same fix to the Postgres `pg_hba` allowlist** while we're there (minor robustness improvement,
  flagged — not silent).
- **Bind ARI (`http.conf`) to loopback + host-gateway, never `0.0.0.0`.** The worker reaches it via
  **`host.docker.internal:8088`** using the `extra_hosts: host-gateway` mapping it already declares for Postgres —
  no new networking primitive.
- **UFW allows `8088/tcp` only from `callmon-net`'s pinned subnet** — same "allowlist this project's bridge
  subnet" rule as `pg_hba`. ARI is never internet-reachable and not reachable from other apps' bridge networks.
- **ARI creds → `.env.prod`** (`ARI_BASE_URL` / `ARI_USERNAME` / `ARI_PASSWORD`), same store as Twilio/GHL;
  `/root/.owen-ari-pw` retired as source of truth (kept only as optional deploy-time render input).
- **Software modularity:** the ARI-WS consumer is a **self-contained worker module behind `ASTERISK_ENABLED`**.
  Flag off → worker behaves exactly as today (Twilio/SignalWire/GHL drain + APScheduler unchanged). Flag on →
  the consumer starts. Additive and reversible; never in the existing ingest path.

### 4. Health & monitoring — additive, non-gating
- **Do NOT fold Asterisk into the existing `/health`** — that check gates the whole app/deploy; an Asterisk blip
  must never be able to fail a Docker deploy and take down the live Twilio dashboard.
- Add a **separate `/health/telephony`** endpoint that always returns 200 + a JSON status body (with a `degraded`
  flag, never 503): ARI reachable, trunk registration state, active channel count, ARI-WS consumer liveness
  (last-event timestamp / connected bool).
- **Monitoring:** a lightweight periodic **APScheduler** check (worker already runs APScheduler) logs/warns on
  trunk-unregistered, ARI-WS-disconnected, RTP-port-exhaustion. No new monitoring infra (same-VPS convention);
  wiring log warnings → an actual alert channel is deferred (fog).
- **`scripts/deploy.sh` healthcheck stays app-only** (`/health`) — Asterisk can never fail a deploy.

### 5. Restart / upgrade — systemd/apt native, decoupled from `make deploy`
- **Config changes never restart** — targeted `reload` only (§1). `restart` is reserved for daemon
  upgrades/crash recovery.
- **Daemon lifecycle stays OS-native** under **systemd** (`systemctl`), managed like the Postgres package —
  **not** pulled into the Docker deploy flow. `make deploy` never restarts Asterisk. Documented split:
  containers = git/Docker deploy; Asterisk daemon = systemd + apt.
- **Planned restarts** use `asterisk -rx "core restart when convenient"` (drains live calls first); hard
  `systemctl restart` only for crash recovery.
- **Crash recovery:** systemd **`Restart=always`** self-heals; the IP-auth trunk re-registers automatically on
  startup; `/health/telephony` + the scheduler warning make the outage visible.
- **Version pin:** `apt-mark hold asterisk` locks the proven **22.10.1** so unattended `apt upgrade` can't jump
  majors and break `chan_pjsip`/ARI. Upgrades become deliberate, tested acts. Config is in git, so a rebuilt host
  restores from repo + `.env.prod`.

### 6. Coexistence guarantee — additive by construction, defaulted-dark, verified, reversible
- **Only shared surfaces:** Postgres (Asterisk writes **new** rows on the same `call_events`→`calls` projection —
  additive, per ticket 05) and the `worker` process (Asterisk consumer is a **separate flag-gated module
  alongside**, not inside, the Twilio drainer / APScheduler). No other shared mutable surface; new host ports
  (SIP 5060, RTP 10000–10200, ARI 8088→bridge), new config files, new health endpoint are all disjoint from
  `/webhooks/twilio/*` and the existing ingest/recording/analysis/GHL-relay code.
- **Firewall change is additive UFW rules only** — must not touch existing Traefik (80/443), SSH, or other-app
  rules; capture `ufw status` before/after.
- **`ASTERISK_ENABLED=false` is the committed default** in `.env.prod.example` — code ships **dark**.
- **Verification (record on rollout):** (1) deploy with flag off → place a real Twilio call → confirm rows +
  GHL relay unaffected; (2) flip flag on → confirm `/health/telephony` green and Twilio drain still running;
  (3) place a real BulkVS call → confirm it lands as an `asterisk`-provider row without perturbing Twilio rows.
- **Rollback:** flip `ASTERISK_ENABLED=false` + reload — instant, no redeploy — reverts to today's behavior.

### Facts downstream tickets depend on
- Config lives in-repo at **`asterisk/`**; deploy = rsync + targeted reload; daemon = systemd/apt (held at 22.10.1).
- Ports: **SIP 5060/udp** (IP-locked to 4 SBC IPs), **RTP 10000–10200/udp** (session-validated, not IP-locked),
  **ARI 8088/tcp** (bridge-subnet-only).
- **`callmon-net` gets a pinned subnet**; ARI + Postgres both allowlist that subnet.
- New env: `ASTERISK_ENABLED` (default false), `ARI_BASE_URL`, `ARI_USERNAME`, `ARI_PASSWORD`.
- New endpoint: **`/health/telephony`** (non-gating).
- **Ticket 13 (WebRTC softphone leg)** must add its **own secured WebRTC transport** (wss + DTLS-SRTP) *separate*
  from this trunk-facing firewall — the browser leg can't ride the IP-locked SIP path; reconcile there.

### Post-close addendum — WebRTC transport + coturn (added by [ticket 13](13-in-platform-webrtc-calling.md), 2026-07-22)
Ticket 13 resolved the "own secured WebRTC transport" flagged above, adding these surfaces to this ticket's
infra/deploy/security scope — all additive, flag-gated under the same `ASTERISK_ENABLED` module, reversible:
- **coturn** — a **second native telephony service** (same native-host + rendered-secret + additive-UFW mold
  as Asterisk), for WebRTC media relay so operators behind UDP-blocking firewalls still get audio. Ports:
  **3478/udp+tcp** (STUN/TURN) and **5349/tcp (TLS)** — critically relays over **443/TLS** for near-universal
  traversal — plus a **TURN relay port range** (own additive UFW rules). TURN creds are **minted short-lived
  by the backend** (alongside the operator SIP creds). Managed by systemd like Asterisk; config/secrets follow
  the `.env.prod` render convention.
- **Asterisk WebSocket for SIP-over-`wss`** — bound **loopback/bridge only** (same treatment as ARI), fronted
  by **Traefik** (the existing TLS edge): browser `wss` → Traefik TLS-terminates → plain `ws`/loopback to
  Asterisk. One new public surface = a `wss` route on a Traefik-owned domain; **no new host cert lifecycle**.
- **Media DTLS-SRTP cert** for the browser leg — generated on the host, rendered at deploy (same secret
  convention). Browser media rides the **existing `10000–10200/udp` RTP range** (no new range); Asterisk
  `icesupport=yes` advertises the VPS public IP as host ICE candidate.
- **Per-operator PJSIP WebRTC endpoints** live in the in-repo `asterisk/pjsip.conf`; **`codec_opus`** added
  for Opus↔ulaw transcoding on the operator leg. New env for the softphone (backend-side): operator SIP + TURN
  credential minting (secrets rendered, not committed).
