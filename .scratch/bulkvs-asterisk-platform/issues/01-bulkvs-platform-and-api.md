# BulkVS platform + API capabilities

Type: research
Status: resolved
Blocked by: —

## Question

What does BulkVS actually give us to build on? Surface the facts every later decision waits on:

- **DID inventory / provisioning API** — endpoints to list owned numbers, search/order/release DIDs, and
  set per-DID routing/trunk. Auth model (API key/basic). Is there a webhook for inventory changes?
- **SIP trunk** — how inbound calls to a BulkVS DID reach our server: trunk auth (IP-ACL vs registration),
  BulkVS signaling/media IP ranges to allow-list, supported codecs (G.711/opus?), DTMF mode, TLS/SRTP support,
  concurrent-channel limits.
- **Outbound** — how we originate calls out through BulkVS (termination), caller-ID rules, any per-call API.
- **Messaging** — SMS **and** MMS: send API, inbound delivery mechanism (webhook payload shape, delivery
  receipts), throughput limits, and **A2P 10DLC / brand+campaign registration** requirements/process.
- **CDR** — does BulkVS expose call detail records / usage via API or portal export? (Relevant to reconciliation.)
- **Existing scaffolding** — the repo already has a provider-agnostic `sync-numbers` CLI (imports Twilio inventory).
  Note what a BulkVS adapter would need to fit it.

## Findings

Resolved by `/research` subagent (mostly from BulkVS's in-portal REST OpenAPI spec + corroborating integrations).

**API:** base `https://portal.bulkvs.com/api/v1.0`, **HTTP Basic auth** (API username+password from portal → API
Credentials). Number ops: `GET /tnRecord` (list owned), `GET /orderTn`+`GET /exchanges` (search), `POST /orderTn`
(buy), `DELETE /tnRecord` (release), `POST /tnRecord` (set per-DID `Trunk Group` / `Custom URI` / `Call Forward` /
`PSTN Failover`). Trunk groups: `GET/PUT/POST/DELETE /trunkGroups` (PUT=IP-auth, POST=registration). Also `/ipHost`,
`/e911Record`, `/accountDetail`. **No inventory webhook → adapter must poll `GET /tnRecord`.**

**SIP trunk:** IP-auth (recommended) or registration. Host `sip.bulkvs.com`; core IPs **162.249.171.198,
76.8.29.198, 69.12.88.198, 199.255.157.198**; **UDP 5060**. Inbound RURI default **11-digit `1NXXNXXXXXX`**
(Delivery Type configurable to E.164/10-digit). Codecs **G.711u / G.729a / T.38**; DTMF **RFC2833 or in-band** (no
SIP INFO). **⚠ TLS/SRTP NOT supported — plain UDP only** (keep BulkVS↔Asterisk on a trusted path; feeds ticket 09).
Concurrency effectively unlimited; CPS not published.

**Outbound:** pure SIP termination (no REST origination); ANI must be 10/11-digit or `+E.164` or it's rejected;
source IP must be registered via `ipHost`/portal Host step. STIR/SHAKEN applied.

**Messaging:** `POST /api/v1.0/messageSend` (`{From, To:[...], Message, delivery_status_webhook_url}`, 11-digit).
Inbound via **Messaging Webhook** (POST JSON `{To:[...], From, Message}`) assigned per-DID; **inbound source IPs
52.206.134.245 and 192.9.236.42** (allow-list on the FastAPI webhook route). MMS supported (media URL must be
HTTPS). Delivery receipts inconsistent/unreliable. **⚠ A2P 10DLC brand+campaign registration is a HARD prerequisite
for outbound to US mobiles (unregistered = 100% blocked since Feb 2025); approval 1–3d brand / 3–15d campaign** →
graduated into a new task ticket (12) that blocks SMS *send*.

**CDR:** **no CDR/usage REST endpoint** — portal export only. → attribution/reconciliation must source call data
from **Asterisk** (CDR/CEL/ARI events), not BulkVS. Confirms ticket 05's direction.

**Caveats:** `messageSend`/inbound-webhook shapes are community-verified (FusionPBX), not spec-confirmed; TLS-unsupported
rests on one 2021 report; 10DLC fees + rate limits not published — all to re-verify from inside the account.

<details>
<summary>Full research report</summary>

Base `https://portal.bulkvs.com/api/v1.0` (spec v1.0.05; mirror `portal2`). No `docs.bulkvs.com`; authoritative
Swagger is login-gated at `/api/v1.0/documentation` + `/openapi`. Old FAQ says SOAP but live API is REST v1.0.

**Inventory/provisioning:** Basic auth (API user+pass). `GET /tnRecord` list; `GET /orderTn`+`GET /exchanges`
search (Npa/Nxx/Lca); `POST /orderTn` buy; `DELETE /tnRecord` release; `POST /tnRecord` route (Trunk Group / Custom
URI / Call Forward / PSTN Failover). `GET/PUT/POST/DELETE /trunkGroups`, `/ipHost`, `/e911Record`+`/validateAddress`,
`/accountDetail`, `POST /twilio` (BYOC). No inventory webhook → poll.

**Inbound SIP:** IP-auth via Interconnection→Host + Trunk Group assigned per DID (or registration). `sip.bulkvs.com`
SRV; IPs 162.249.171.198 / 76.8.29.198 / 69.12.88.198 / 199.255.157.198; UDP 5060. RURI 11-digit `1NXXNXXXXXX`
default (configurable). Codecs G.711u/G.729a/T.38. DTMF RFC2833 or inband. TLS/SRTP not supported (single 2021
report). Channels ~unlimited; CPS unpublished.

**Outbound:** SIP only to `sip.bulkvs.com`; formats `1NXXNXXXXXX@`, `NXXNXXXXXX@`, `911@`. ANI 10/11-digit or +E.164
else rejected; STIR/SHAKEN + robocall mitigation.

**Messaging:** `POST messageSend` (Basic, JSON `{From:"1...", To:["1..."], Message, delivery_status_webhook_url}`;
`To` is array). Inbound webhook (Messaging→Messaging Webhooks, per-DID under Inbound→DIDs–Manage); POST JSON
`{To:[...], From, Message}`; from IPs 52.206.134.245 / 192.9.236.42. MMS media must be HTTPS. Delivery receipts
unreliable (Textable disables them). Throughput unpublished. A2P 10DLC: register Brand (~$4 + opt $40 vet) + Campaign
(~$15 + $1.50–10/mo, T-Mobile +$50) via TCR; `/tnRecord` carries TCR fields; you register (portal as CSP); brand
1–3 business days, campaign ~3–15; hard block on unregistered US-mobile traffic since Feb 2025. BulkVS-specific
10DLC fees not found.

**CDR:** none in REST — portal UI/export only. Source CDR from Asterisk.

**Adapter needs:** store API user+pass (Basic); sync via `/tnRecord`+`/orderTn`+`/exchanges` (poll, no webhook);
bind routing via `POST /tnRecord` Trunk Group → Asterisk SBC IP; expect UDP/5060, ulaw, RFC2833, 11-digit RURI;
outbound SIP-only + register source IP + enforce ANI format; SMS/MMS via `messageSend`+inbound webhook (allow-list
the two IPs), block outbound until 10DLC approved; CDR from Asterisk.

**Gaps:** auth scheme inferred (not in machine spec); messageSend/inbound shapes community-sourced; TLS-unsupported
single report; CPS/throughput/10DLC fees unpublished; portability + full messaging/CDR paths need re-verify from
inside the login-gated Swagger.

</details>
