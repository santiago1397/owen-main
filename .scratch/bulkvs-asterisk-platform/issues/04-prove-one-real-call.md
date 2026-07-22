# Stand up BulkVS↔Asterisk and prove one real inbound call via ARI

Type: task
Status: open (driven as far as AFK-possible; blocked on account holder — see Answer)
Blocked by: 01, 02
Claimed by: wayfinder session 2026-07-22

## Question

Not a decision — the enabling task that grounds every infra/data-model choice in reality. Get **one real
inbound call** to a BulkVS DID to land on the native Asterisk, enter a Stasis app, be answered + recorded
under ARI control, and hang up cleanly. Then the reverse: originate **one outbound** call via ARI.

Drives out the true SIP/NAT/codec/firewall requirements that tickets 05, 06, and 09 depend on. Agent drives
what it can over the `dispatch` ssh alias; otherwise hands the user a precise, ordered checklist (trunk config,
allow-listed IPs, ARI user, dialplan hand-off, test-call steps).

### Progress

**Session 2026-07-22 — inspected the live server over `dispatch`; drove every AFK step. The remaining work is
human-only (BulkVS account + buy a DID + place a real call), so the task stays open.**

#### Server facts discovered (all read-only)

- **Host:** Ubuntu 24.04.4. Public **IPv4 = `144.126.138.157`** (this is the SBC IP BulkVS must send to and
  the source IP we register with BulkVS). Also has IPv6 `2605:a140:2339:1443::1` — ignore for the SIP trunk.
- **Asterisk `22.10.1`**, running as an active systemd service (`/usr/sbin/asterisk -mqf`). ✅ This clears
  **every** version floor ticket 02 flagged: external-media (16.6+), `chan_websocket` (20.16+), AudioSocket (18+).
  The "verify installed version early" warning from ticket 02 is now resolved — no upgrade needed for the AI path.
- **Modules loaded:** `res_pjsip`, `chan_pjsip`, and 12 `res_ari*` modules all Running.
- **Firewall (ufw):** already open for telephony — `5060/udp` and RTP `10000:20000/udp` (v4+v6). ✅ No firewall
  work needed to *receive* a call. (Ticket 09 will want to *tighten* 5060 to the 4 BulkVS IPs only — right now
  it's `ALLOW Anywhere`, which is fine for the one-call test but a hardening item for 09.)
- **Prior scaffolding (Jul 21, from an earlier session):**
  - `/etc/asterisk/pjsip.conf` — a complete BulkVS **IP-auth trunk template** (`[bulkvs]` endpoint/identify/aor,
    `disallow=all; allow=ulaw,alaw`, `direct_media=no`, `rtp_symmetric=yes`, `force_rport`, `rewrite_contact`,
    `identify_by=ip`) but with **placeholder match/contact** (`BULKVS_SIP_IP` / `BULKVS_SIP_HOST`). Deliberately
    left inert ("5060 open but useless until the real IP is set"). **No pjsip endpoint is live** (`pjsip show
    endpoints` empty) because the placeholders aren't valid — config needs the real IPs + a reload.
  - `/etc/asterisk/ari.conf` — `[general] enabled = yes` but **no ARI user defined** (`ari show users` empty).
  - `/etc/asterisk/http.conf` — `bindaddr=127.0.0.1` (good, localhost-only) but **HTTP server is DISABLED**
    (`http show status` → "Server Disabled"). ARI rides the HTTP server, so **ARI is currently unreachable**.
  - `extensions.conf` — **no `from-bulkvs` context / no Stasis hook**, so an inbound call has nowhere to go.

#### The real blocker

**There is no BulkVS account or API credential anywhere** — not in the repo, not in any `/opt/*/.env` on the
server. Proving "one real call" is impossible until a human: (a) confirms/creates a BulkVS account, (b) buys a
DID, (c) points it at `144.126.138.157`, and (d) physically dials it. A background agent can't do any of those.
Everything an agent *can* do is either already scaffolded or specified below, ready to apply in one sitting.

#### Ready-to-apply config (drop-in, uses the real IPs from ticket 01)

Apply these **together with** buying the DID so the test call can validate immediately — don't apply them days
early (unvalidated prod telephony config + a live ARI credential sitting idle is pure downside).

**1. `pjsip.conf` — replace the placeholders** (BulkVS core IPs from ticket 01; outbound host `sip.bulkvs.com`):

```ini
[bulkvs]
type=identify
endpoint=bulkvs
match=162.249.171.198
match=76.8.29.198
match=69.12.88.198
match=199.255.157.198

[bulkvs]
type=aor
contact=sip:sip.bulkvs.com:5060   ; outbound termination target (ticket 01: SIP-only, no REST origination)
qualify_frequency=60
max_contacts=1
```

Codecs: keep `allow=ulaw` (G.711u — ticket 01). DTMF: BulkVS is RFC2833 (default for chan_pjsip). **No TLS/SRTP**
(ticket 01) — plain UDP, which is why ticket 09 must keep BulkVS↔Asterisk on a trusted/allow-listed path.

**2. `http.conf` — enable the HTTP server (ARI depends on it), localhost-only:**

```ini
[general]
enabled=yes
bindaddr=127.0.0.1
bindport=8088
```

**3. `ari.conf` — add a user** (store the password in the app/worker secrets later — that binding is ticket 09):

```ini
[owen]
type=user
read_only=no
password = <generate a strong secret>
password_format = plain
```

**4. `extensions.conf` — hand inbound BulkVS calls to a Stasis app** (context matches `from-bulkvs` in pjsip):

```ini
[from-bulkvs]
; BulkVS sends 11-digit 1NXXNXXXXXX RURI by default (ticket 01)
exten => _1NXXNXXXXXX,1,NoOp(Inbound BulkVS call to ${EXTEN})
 same => n,Stasis(owen-test)
 same => n,Hangup()
exten => _NXXNXXXXXX,1,Goto(from-bulkvs,1${EXTEN},1)   ; also accept 10-digit if Delivery Type is changed
```

**5. Minimal test Stasis app** — answer + record + hang up, to prove ARI control end-to-end. Any small ARI
client works (Python `ari` / `aioari`, or a curl-driven `POST /channels/{id}/answer` + `POST /channels/{id}/record`
against the StasisStart websocket event on `ws://127.0.0.1:8088/ari/events?app=owen-test`). This throwaway app is
*not* the product flow-engine (that's ticket 06) — it exists only to validate the pipe.

Reload after edits: `asterisk -rx "pjsip reload"; asterisk -rx "module reload res_http"; asterisk -rx "core reload"`.
Verify: `pjsip show endpoints` shows `bulkvs Unavailable→Reachable` once the aor qualifies; `ari show users` lists `owen`.

#### Ordered checklist for the account holder (HITL — the part only you can do)

1. **BulkVS account** — confirm one exists, or sign up at `portal.bulkvs.com`. Generate **API credentials**
   (Basic auth user+pass) under API Credentials — later tickets (07 sync, 08 SMS) need these; stash them where
   the app reads secrets. *(Record the credential location here when done.)*
2. **Register our SBC as a Host** — Interconnection → Host / `ipHost`: add source IP **`144.126.138.157`** (UDP).
   Required for **outbound** (BulkVS rejects termination from unregistered IPs — ticket 01).
3. **Create a Trunk Group** (IP-auth) pointing to **`144.126.138.157:5060` UDP**.
4. **Buy one DID** — `GET /orderTn`/`exchanges` to search, `POST /orderTn` to buy (or portal UI).
5. **Route the DID** — `POST /tnRecord` set its **Trunk Group** to the one from step 3 (so inbound rings our box).
   Confirm Delivery Type = 11-digit `1NXXNXXXXXX` (matches the dialplan above).
6. **Apply the 4 config blocks above** on the server (via `dispatch`) and reload; start the `owen-test` Stasis app.
7. **Place the inbound test call** to the DID from any phone → expect: channel hits `from-bulkvs` → `StasisStart`
   on `owen-test` → answered + recorded → clean hangup. Grab `/var/log/asterisk/full` + the recording to confirm.
8. **Originate the outbound test** via ARI: `POST /ari/channels` with
   `endpoint=PJSIP/1<10-digit-cell>@bulkvs`, `app=owen-test`, and a valid 10/11-digit or +E.164 `callerId`
   (unregistered/blank ANI is rejected — ticket 01). Answer on your cell → clean hangup.

#### Facts later tickets will inherit once the call succeeds (fill in on completion)

- Whether `direct_media=no` + `rtp_symmetric` is sufficient for NAT, or `external_media_address` is needed.
- Actual inbound RURI/Delivery-Type observed, and the ARI event → `call_events` mapping seen live (feeds ticket 05).
- Recording file path/format produced under ARI control (feeds ticket 05/06 + voicemail reuse).
- Any firewall/codec/NAT surprises (feeds ticket 09).
