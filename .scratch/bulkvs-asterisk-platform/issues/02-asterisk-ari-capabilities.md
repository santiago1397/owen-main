# Asterisk / ARI capabilities + BulkVS trunk config

Type: research
Status: resolved
Blocked by: —

## Question

Confirm Asterisk can be the programmable "brain" for every flow the destination needs, and how it wires to
BulkVS and to our backend:

- **ARI primitives** — does the Asterisk Restinterface (Stasis) cover: answer, playback/prompt, **record**,
  **bridge/dial** (for forwarding + ring groups), **DTMF gather** (for IVR menus), **originate** (outbound),
  and **external-media / audiofork** (stream call audio out to our app for AI agents)? Note version needed.
- **Trunk config** — recommended `chan_pjsip` endpoint/identify/aor config to peer with BulkVS (IP-auth trunk),
  inbound dialplan that hands calls to a Stasis app, codecs, and how to originate outbound through the trunk.
- **Backend↔ARI wiring** — how a Dockerised FastAPI/worker reaches ARI on the host (ARI websocket + REST over
  `localhost`/`host.docker.internal`), ARI user/password/TLS, event stream reliability, reconnection.
- **Server reality** — Asterisk is installed **native, idle** on the prod server (ssh alias `dispatch`). What is
  the installed version, is ARI/`res_ari` enabled, and what config already exists? (Read-only inspection; do not
  change prod. If SSH is unavailable to the subagent, document exactly what to check and record it as a gap.)

## Findings

Resolved by `/research` subagent. Summary of what's confirmed against docs.asterisk.org (full report below).

**Verdict: Asterisk + ARI covers every primitive the destination needs.** ARI is GA since Asterisk 12;
answer / playback / record / bridge+dial / DTMF-gather / originate all exist there. The AI-agent audio path
(**external media**) needs 16.6+, and the low-friction transports are newer: **AudioSocket** (18+) or the
**`chan_websocket`** driver (20.16 / 22.6 / 23.0). Recommendation: on a recent Asterisk, use `chan_websocket`
external media for AI agents (no RTP timing code); else AudioSocket.

**Key build facts:**
- Inbound: dialplan `Stasis(call-brain,...)` hands the channel to our app; `StasisStart`/`StasisEnd` +
  `ChannelDtmfReceived` are the core events. DTMF events need `dtmf_events` flag on mixing bridges.
- Call recording = record the **bridge** (mixed two-party) → files under `/var/spool/asterisk/recording`.
- Forwarding/ring-groups = holding-bridge (MoH) while originating target legs → move to mixing bridge on answer.
- BulkVS trunk = **IP-authenticated** `chan_pjsip` endpoint+aor+**identify** (no registration/auth). Keep
  `direct_media=no` and `disallow=all; allow=ulaw` so RTP anchors on Asterisk (required for record + external media).
- Backend↔ARI: HTTP+WebSocket on `:8088/ari`, creds from `ari.conf`; Dockerised app reaches host Asterisk via
  `host.docker.internal:host-gateway`; bind ARI to localhost/bridge IP + firewall — never public. WS drops lose
  events → resync via `GET /channels` + `/bridges` on reconnect; dialplan must have a `Hangup()` fallback.
- Health: `pjsip show endpoints/aors/identifies` (trunk) + `GET /ari/channels` (live calls).

**Gaps flagged for ticket 04 (prove-one-call) and ticket 01 (BulkVS):** exact BulkVS SIP host/FQDN + full
signaling IP/CIDR list for `identify match=` + outbound caller-ID DID format are NOT in Asterisk docs — confirm
in the BulkVS portal. Also **verify the installed Asterisk version** on the server (`core show version`,
`module show like websocket`) before committing to an external-media transport; a community report flags garbled
audio for ExternalMedia+AudioSocket-over-ARI on 22.8 — validate that path early.

<details>
<summary>Full research report</summary>

# Asterisk ARI / Stasis Capabilities — Research Report

Scope: driving a **native Asterisk** (host) from a **Dockerized Python/FastAPI + Postgres** app via ARI, with a **BulkVS IP-authenticated SIP trunk** on `chan_pjsip`.

## 1. ARI Primitives (Stasis)

ARI is a REST + WebSocket interface: REST calls issue commands to resources (channels, bridges, playbacks, recordings), and an outbound WebSocket delivers asynchronous events. A channel enters your app when the dialplan runs `Stasis(app-name)`, which fires a `StasisStart` event carrying the channel id.

- **Answer**: `POST /channels/{id}/answer` (ARI GA in Asterisk 12).
- **Playback**: `POST /channels/{id}/play?media=sound:...` (also `recording:`,`number:`,`digits:`,`tts:`); works on bridges too.
- **Record**: `POST /channels/{id}/record` and `POST /bridges/{id}/record`. Live (mute/pause/stop) or stored (`/var/spool/asterisk/recording`). Record the **bridge** for mixed two-party audio.
- **Bridge+dial**: create bridge (`mixing`/`holding`), originate leg(s), `addChannel`/`removeChannel`. Holding bridge w/ MoH while dialing; mixing bridge on answer. Simul-ring = originate several, keep first answer; sequential = one at a time w/ timeouts.
- **DTMF gather**: subscribe `ChannelDtmfReceived` (fires on digit end). Mixing-bridge channels need `POST /bridges?type=mixing,dtmf_events`. Send via `POST /channels/{id}/dtmf`.
- **Originate**: `POST /channels` with `endpoint=PJSIP/${num}@trunk`; route answered leg to dialplan OR Stasis app; supports `callerId`,`timeout`,`variables`.
- **External media / AudioFork** (AI agents): `POST /channels/externalMedia`, added to a mixing bridge; audio injected back via `UNICASTRTP_LOCAL_ADDRESS/PORT`. Introduced 16.6.0. Transports: RTP/UDP (16.6), AudioSocket (18+), `chan_websocket` (20.16/21.11/22.6/23.0 — Asterisk handles frame timing; recommended when available).

Sources: ARI getting-started, media (recording/playback), bridges/holding-bridges, DTMF, Channels REST API, ari-examples repo, External-Media-and-ARI, WebSocket channel-driver doc.

## 2. BulkVS Trunk (chan_pjsip) — IP-authenticated pattern

```ini
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0

[bulkvs]
type=endpoint
context=from-bulkvs
transport=transport-udp
disallow=all
allow=ulaw
allow=alaw
aors=bulkvs
direct_media=no

[bulkvs]
type=aor
contact=sip:<bulkvs-sip-host>:5060
qualify_frequency=60

[bulkvs-identify]
type=identify
endpoint=bulkvs
match=<bulkvs-signaling-ip>
```

Inbound → Stasis:
```ini
[from-bulkvs]
exten => _X.,1,NoOp(Inbound from BulkVS to ${EXTEN})
 same => n,Answer()
 same => n,Stasis(call-brain,${EXTEN},${CALLERID(num)})
 same => n,Hangup()
```
Outbound: `Dial(PJSIP/${EXTEN}@bulkvs)` or ARI `POST /ari/channels?endpoint=PJSIP/+1...@bulkvs&app=call-brain&callerId=+1YOURDID`.
Codecs: US = `ulaw` (add `alaw`); `direct_media=no` to anchor RTP; open RTP range (rtp.conf 10000–20000/UDP); NAT vars only if behind NAT.

## 3. Backend ↔ ARI (Docker → host)
- REST `http://host:8088/ari/...`, events WS `ws://host:8088/ari/events?api_key=user:password&app=call-brain`.
- Creds from `ari.conf`; use a strong password.
- Container→host: `extra_hosts: ["host.docker.internal:host-gateway"]`, point at `http://host.docker.internal:8088`. Or `network_mode: host`.
- WS reconnection: exponential backoff+jitter; miss events while disconnected → resync via `GET /channels`+`/bridges`; keepalive pings; dialplan `Hangup()` fallback so calls don't orphan if backend down; use a maintained client lib.

## 4. Ops
- Enable: `http.conf` (`enabled=yes`, `bindaddr=127.0.0.1`, `bindport=8088`) + `ari.conf` (`type=user`,`password=`); `module reload res_ari.so http.so`. Transport changes need full restart.
- Secure to localhost/bridge IP + firewall 8088 to docker subnet; optional TLS.
- Health: `GET /ari/channels`/`/bridges`; `pjsip show endpoints/aors/contacts/identifies` for trunk state.

## Gaps / Unverified
- Exact BulkVS SIP host/IPs/caller-ID format — confirm in BulkVS portal (not in Asterisk docs).
- External-Media doc page is stale on transports; confirm installed Asterisk version + `chan_websocket` availability.
- Community bug report: ExternalMedia+AudioSocket over ARI on 22.8 garbled — validate AI path early; `chan_websocket` may be safer.
- Docker host-gateway is standard Docker behavior, not Asterisk-doc-sourced.

</details>
