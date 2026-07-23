# Asterisk + BulkVS — deploy runbook

Native Asterisk on the OWEN VPS, providing a BulkVS SIP trunk as a **third** telephony
provider alongside the live Twilio / SignalWire / GHL integrations. It ships **dark**:
the backend does nothing with it until `ASTERISK_ENABLED=true`, and the container
healthcheck never depends on it. `GET /health/telephony` is the non-gating probe that
tells you whether it came alive.

> **Scope:** this directory is the in-repo deliverable (config templates + this runbook).
> Provisioning the VPS is a manual deploy step — nothing here SSHes anywhere.

## What's here

| File | Purpose |
|------|---------|
| `pjsip.conf` | BulkVS trunk **+ per-operator WebRTC softphone endpoints** (Ticket 13): trunk transport/endpoint/auth/aor/identify/registration, plus the `transport-wss` + `operator-webrtc` template and per-operator endpoint trio |
| `ari.conf` | ARI user the backend authenticates as |
| `http.conf` | HTTP server hosting ARI + the ARI WebSocket (bind + port) |
| `rtp.conf` | RTP media port range (10000-10200) **+ ICE/STUN for WebRTC media (reuses the same range)** |
| `turnserver.conf` | **coturn** TURN/STUN media relay over TLS/443 for operators behind restrictive firewalls (Ticket 13); ephemeral `use-auth-secret` creds minted by the backend |
| `extensions.conf` | Minimal dialplan: inbound → Stasis(`${ARI_APP}`), outbound → trunk |
| `cdr.conf` | CDR engine on, `unanswered=yes` so missed calls also get a CDR row |
| `cdr_pgsql.conf` | Writes CDRs into OWEN's Postgres `cdr` table (read by the CDR reconciler) |

All `*.conf` are **templates**: `${VAR}` tokens are filled from `.env.prod` at deploy
time. Secrets (`BULKVS_SIP_PASSWORD`, `ARI_PASSWORD`) live only in `.env.prod`
(chmod 600) — never committed.

## Config / env

Add to `.env.prod` (documented in `.env.prod.example`):

```
ASTERISK_ENABLED=false        # master switch — leave false until the host is up
ARI_HOST=host.docker.internal # how the backend reaches ARI (docker host-gateway)
ARI_PORT=8088
ARI_USERNAME=owen
ARI_PASSWORD=<generate: openssl rand -hex 24>
ARI_APP=owen
ARI_BIND_ADDR=<callmon-net gateway IP, e.g. 172.20.0.1>  # deploy-render only (http.conf)
BULKVS_TRUNK_NAME=bulkvs
BULKVS_SIP_USERNAME=<from BulkVS portal>
BULKVS_SIP_PASSWORD=<from BulkVS portal>
BULKVS_FROM_NUMBER=+1XXXXXXXXXX
ASTERISK_SPOOL_DIR=/data/asterisk-spool        # in-CONTAINER path (recordings bind-mount)
ASTERISK_CDR_DB_USER=asterisk_cdr              # deploy-render only (cdr_pgsql.conf)
ASTERISK_CDR_DB_PASSWORD=<generate: openssl rand -hex 24>  # deploy-render only

# --- Operator WebRTC softphone (Ticket 13) ---
ASTERISK_PUBLIC_IP=<VPS public IP>             # deploy-render only (pjsip transport-wss + rtp ICE + coturn)
STUN_SERVER=stun.l.google.com:19302            # deploy-render only (rtp.conf stunaddr)
OPERATOR_SIP_SECRET=<generate: openssl rand -hex 24>   # backend + pjsip: per-operator WebRTC digest password
OPERATOR_SIP_DOMAIN=api.<APP_DOMAIN>           # SIP realm (also coturn realm)
OPERATOR_WSS_URL=wss://api.<APP_DOMAIN>/ws     # Traefik-fronted Asterisk WebSocket (public)
OPERATOR_SIP_TTL_SECONDS=3600
OPERATOR_SLUG_EXAMPLE=<operator email slug, e.g. jane-example.com>  # deploy-render only (pjsip example endpoint)
TURN_STATIC_SECRET=<generate: openssl rand -hex 32>    # backend + coturn: MUST match on both
TURN_URLS=turns:turn.<APP_DOMAIN>:443?transport=tcp,stun:turn.<APP_DOMAIN>:443
TURN_TTL_SECONDS=3600
TURN_TLS_CERT=/etc/coturn/certs/fullchain.pem  # deploy-render only (coturn TLS)
TURN_TLS_KEY=/etc/coturn/certs/privkey.pem     # deploy-render only (coturn TLS)
```

The backend reads `OPERATOR_SIP_SECRET`, `OPERATOR_SIP_DOMAIN`, `OPERATOR_WSS_URL`,
`OPERATOR_SIP_TTL_SECONDS`, `TURN_STATIC_SECRET`, `TURN_URLS`, `TURN_TTL_SECONDS` (to MINT
softphone creds). `ASTERISK_PUBLIC_IP`, `STUN_SERVER`, `OPERATOR_SLUG_EXAMPLE`,
`TURN_TLS_CERT`, `TURN_TLS_KEY` are render-only (pjsip/rtp/coturn on the host). `TURN_STATIC_SECRET`
MUST be identical in `.env.prod` (backend) and the rendered `turnserver.conf` (coturn) — the
backend HMACs creds that coturn verifies.

The backend reads `ASTERISK_SPOOL_DIR` (recordings fetch) but NOT `ARI_BIND_ADDR`,
`ASTERISK_CDR_DB_USER`, or `ASTERISK_CDR_DB_PASSWORD` — those are only used when rendering
`http.conf` / `cdr_pgsql.conf` on the host.

## Recordings (Ticket 05) — one pipeline, local move not download

Flow nodes with a `record` modifier drive ARI `record` (WAV). Asterisk writes those WAVs to
its recording spool on the host; OWEN reuses the EXISTING recordings pipeline
(recordings table → fetch → transcribe → analyze — the same path a Twilio recording takes),
so there is exactly one recording system. Because the audio is already local, the "fetch"
is a file **move**, not an HTTP download.

Bind-mount the host spool dir into the **app + worker** containers (read-only is fine):

```
host  /var/spool/asterisk/recording   ->   container  /data/asterisk-spool   (ro)
```

`ASTERISK_SPOOL_DIR` must equal the in-container mount target (`/data/asterisk-spool`).
The `RecordingFinished` ARI event carries the recording `name` (which the interpreter set to
`{linkedid}-{tag}-{n}`); OWEN registers a recordings row keyed on that name and enqueues a
`recording_fetch` that copies `${ASTERISK_SPOOL_DIR}/<name>.wav` into `RECORDINGS_DIR`, then
the identical transcribe/analyze chain runs. (Asterisk's default recording dir is
`/var/spool/asterisk/recording`; if yours differs, mount that dir instead.)

## Flow prompt TTS (Ticket 15) — shared sounds path

Flow graphs store prompts as plain **text**; the backend synthesizes them with OpenAI TTS
(`TTS_MODEL`/`TTS_VOICE`, reusing `OPENAI_API_KEY`) into 8kHz-mono 16-bit WAVs cached at
`<RECORDINGS_DIR>/tts/<sha256(text|voice)>.wav` — synthesized at flow activation
(best-effort prewarm) and lazily at call time on a cache miss.

Playback uses an **absolute-path `sound:` URI**: per Asterisk's media-URI rules a sound URI
may carry a full filesystem path **without the extension** (`sound:/data/recordings/tts/
<sha256>`), and Asterisk resolves the codec extension itself — this is what OWEN sends, so
no `sounds` directory registration or `extensions.conf` change is needed. What IS needed:
**Asterisk (native, on the host) must be able to read that exact path.**

`RECORDINGS_DIR=/data/recordings` is a **named docker volume** (`recordings` in
`docker-compose.prod.yml`, mounted into the app + worker containers at `/data/recordings`).
On the host that volume's data lives at
`/var/lib/docker/volumes/callmon_recordings/_data`, so the host must expose it at the SAME
absolute path the containers use. One-time setup:

```bash
# Make the container path exist on the host, pointing at the volume's data:
mkdir -p /data
ln -s /var/lib/docker/volumes/callmon_recordings/_data /data/recordings
# (a bind mount works too: mount --bind /var/lib/docker/volumes/callmon_recordings/_data /data/recordings)

# Asterisk (runs as the asterisk user) needs read access to the tts/ subdir:
ls -l /data/recordings/tts/    # after the first activation prewarm; ensure o+r or asterisk-group readable
```

Verify: activate a flow with a text prompt, confirm a `<sha256>.wav` appears under
`/data/recordings/tts/`, call the DID and hear it. If playback is silent, check the
Asterisk CLI for "file does not exist" — that means the host path mapping above is missing
or unreadable by the `asterisk` user. A TTS/playback failure never dead-airs a call: the
flow simply continues without that prompt.

## CDR reconcile (Ticket 05) — Asterisk CDR → Postgres

The live ARI-WebSocket consumer can miss a call's terminal event (worker restart mid-call,
or the entry channel leaving Stasis so its `ChannelDestroyed` never arrives). Asterisk's CDR
engine records every call regardless, so `cdr_pgsql` writes CDR rows into a `cdr` table in
**the same Postgres database** OWEN uses, and `app/workers/asterisk_cdr.py` (scheduled every
`ASTERISK_CDR_POLL_SECONDS`, gated on `ASTERISK_ENABLED`) reads recent rows and projects them
into the same `calls`/`call_events` projection. It is idempotent: each row is keyed
`"{linkedid}:{status}"` — the same dedup key the live WS path uses — so re-scans and
WS-vs-CDR overlap never double-count.

Create the `cdr` table once (Asterisk owns it — there is NO Alembic migration for it) and a
least-privilege role for Asterisk to write it:

```sql
CREATE ROLE asterisk_cdr LOGIN PASSWORD '<ASTERISK_CDR_DB_PASSWORD>';
CREATE TABLE cdr (
  id          bigserial PRIMARY KEY,
  start       timestamptz,
  answer      timestamptz,
  "end"       timestamptz,
  clid        text,
  src         text,
  dst         text,
  dcontext    text,
  channel     text,
  dstchannel  text,
  lastapp     text,
  lastdata    text,
  duration    integer,
  billsec     integer,
  disposition text,
  amaflags    integer,
  accountcode text,
  uniqueid    text,
  linkedid    text,
  userfield   text
);
GRANT INSERT, SELECT ON cdr TO asterisk_cdr;
GRANT USAGE, SELECT ON SEQUENCE cdr_id_seq TO asterisk_cdr;
CREATE INDEX ix_cdr_start ON cdr (start);
```

`cdr_pgsql` is adaptive: it writes only columns that exist, so `linkedid`/`uniqueid`/
`answer`/`end` MUST be present (the reconciler reads them). OWEN's `callmon` app role only
needs `SELECT` on `cdr`.

## Operator WebRTC softphone (Ticket 13) — in-platform calling

Operators answer platform calls **in the browser**: a per-operator `chan_pjsip` WebRTC
endpoint (SIP.js, `wss` + DTLS-SRTP), one channel under the call's `Linkedid` — a SEPARATE
seam from the AI external-media path. The frontend in-call bar (answer/hangup/hold/blind-
transfer) lives on the Calls **Platform** tab.

**Control split (non-negotiable):** SIP.js drives ONLY its own leg (INVITE/answer/BYE).
**ALL bridge/hold/blind-transfer go through the BACKEND over ARI** (`POST
/api/telephony/control/*`), NEVER browser→ARI. Blind transfer only for v1 (attended is out
of scope); targets are a DID, another operator, or the AI-agent runtime.

**Auth / creds:** `POST /api/telephony/webrtc/credentials` (authenticated — the app-login
gate is the real boundary) mints short-lived **SIP** (the `operator-<slug>` endpoint +
`${OPERATOR_SIP_SECRET}` digest password) **+ ephemeral coturn TURN** creds. The `<slug>`
matches `app/telephony/credentials.operator_slug(email)`; add one endpoint/auth/aor trio per
operator in `pjsip.conf` (see the `operator-${OPERATOR_SLUG_EXAMPLE}` example).

**Transport:**
- **Signalling `wss` is fronted by Traefik** — no new cert lifecycle. Asterisk's WS binds the
  loopback/host-gateway (`transport-wss` on `${ARI_BIND_ADDR}:8089`, like ARI); add a Traefik
  router that terminates TLS on the app cert and proxies the public path (e.g.
  `wss://api.<APP_DOMAIN>/ws`, `OPERATOR_WSS_URL`) to that internal socket:

  ```yaml
  # Traefik dynamic config (labels or file): route the softphone WS to Asterisk.
  http:
    routers:
      asterisk-ws:
        rule: "Host(`api.<APP_DOMAIN>`) && Path(`/ws`)"
        entryPoints: [websecure]
        service: asterisk-ws
        tls: {}                       # reuse the existing ACME cert — no new cert
    services:
      asterisk-ws:
        loadBalancer:
          servers:
            - url: "http://<ARI_BIND_ADDR>:8089"   # Asterisk transport-wss (loopback/gateway)
  ```
- **Media is DTLS-SRTP straight to the EXISTING `10000-10200/udp` RTP range** — no new range.
  ICE advertises the **VPS public IP** (`${ASTERISK_PUBLIC_IP}` on `transport-wss` +
  `rtp.conf` `stunaddr`). Operators behind restrictive firewalls relay through **coturn over
  TLS/443** (`turnserver.conf`); the backend mints ephemeral TURN creds
  (`use-auth-secret` HMAC — `TURN_STATIC_SECRET` MUST match backend↔coturn) and SIP.js uses
  them as `iceServers`.

**Install coturn** (`apt-get install coturn`), render `turnserver.conf` (below), point its
TLS at the app's existing cert (`TURN_TLS_CERT`/`TURN_TLS_KEY` — reuse Traefik's ACME cert or
the host cert), and `systemctl enable --now coturn`.

## Deploy: render + rsync + targeted reload

1. **Install & pin Asterisk 22.10.1** on the host, then freeze it so it never
   auto-upgrades under you:
   ```
   apt-mark hold asterisk
   ```

2. **Render** the templates from `.env.prod` and **rsync** into `/etc/asterisk/`
   (plus `turnserver.conf` into `/etc/coturn/`):
   ```bash
   set -a; . /opt/santiagoproperties/owen-main/.env.prod; set +a
   for f in pjsip ari http rtp extensions cdr cdr_pgsql; do
     envsubst < asterisk/$f.conf > /tmp/$f.conf
   done
   rsync -a /tmp/{pjsip,ari,http,rtp,extensions,cdr,cdr_pgsql}.conf /etc/asterisk/
   envsubst < asterisk/turnserver.conf > /tmp/turnserver.conf
   rsync -a /tmp/turnserver.conf /etc/coturn/turnserver.conf
   ```

3. **Targeted reload — never restart** (a restart drops in-flight calls):
   ```bash
   asterisk -rx "pjsip reload"
   asterisk -rx "dialplan reload"
   asterisk -rx "module reload res_ari.so res_http_websocket.so"
   asterisk -rx "module reload cdr_pgsql.so"
   asterisk -rx "cdr reload"
   systemctl restart coturn           # coturn has no live-reload; safe (it holds no calls)
   ```

## systemd unit

`/etc/systemd/system/asterisk.service` (excerpt):

```ini
[Service]
ExecStart=/usr/sbin/asterisk -f -C /etc/asterisk/asterisk.conf
Restart=always
RestartSec=2
```

`Restart=always` so the daemon self-heals; combined with `apt-mark hold` the version
stays pinned at 22.10.1 across reboots and upgrades.

## Firewall (UFW)

Same lock-down philosophy as Postgres `pg_hba` — nothing telephony-related is open to
the world:

| Port | Rule |
|------|------|
| `5060/udp` (SIP) | allow **only** from the 4 BulkVS SBC IPs: `162.249.171.198`, `76.8.29.198`, `69.12.88.198`, `199.255.157.198` |
| `10000-10200/udp` (RTP) | open, but **session-validated** by Asterisk (media arrives from a different range, `152.188.166.x`, so it can't be IP-pinned) |
| `8088/tcp` (ARI) | bound to loopback + host-gateway (`http.conf`), UFW-allowed **only** from the pinned `callmon-net` docker subnet |
| `8089/tcp` (Asterisk WS) | bound to loopback/host-gateway (`transport-wss`), reached only via the Traefik `wss` router — **never** opened to the world directly |
| `443/tcp`+`443/udp` (coturn) | open — coturn's TLS relay must be reachable by operator browsers behind restrictive firewalls (TURN over 443 is the whole point) |

```bash
for ip in 162.249.171.198 76.8.29.198 69.12.88.198 199.255.157.198; do
  ufw allow from $ip to any port 5060 proto udp
done
ufw allow 10000:10200/udp
ufw allow from <callmon-net subnet, e.g. 172.20.0.0/16> to any port 8088 proto tcp
# Ticket 13 — coturn TLS relay (operators anywhere); 8089 stays internal (Traefik-only).
ufw allow 443/tcp
ufw allow 443/udp
```

> **Note:** if Traefik already owns `443/tcp` on the host, run coturn on a dedicated IP or a
> separate `tls-listening-port` and point `TURN_URLS` at it. On this single-IP VPS, coturn's
> TLS relay and Traefik can share `443` only if bound to different addresses — pick whichever
> your host layout allows and update `TURN_URLS` to match.

## Verify

With the flag **off**, `/health/telephony` returns the disabled snapshot and the app is
unchanged:
```json
{"asterisk_enabled": false, "ari_reachable": false, "trunk_registered": false}
```

Flip `ASTERISK_ENABLED=true`, `docker compose up -d app`, then:
```bash
curl -fsS https://api.<APP_DOMAIN>/health/telephony
# {"asterisk_enabled": true, "ari_reachable": true, "trunk_registered": true}
```

`ari_reachable` proves the backend reached ARI over the host-gateway; `trunk_registered`
reflects the BulkVS PJSIP endpoint state (`online` via `asterisk -rx "pjsip show
endpoints"`). To revert entirely: set `ASTERISK_ENABLED=false` and redeploy `app` — no
telephony consumers run and every existing path is untouched.
