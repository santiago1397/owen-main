# Quo (OpenPhone) webhook — setup and contract

Module: `backend/app/integrations/openphone/webhook.py` (receiver + processing),
`contact_book.py` (Quo's contact names). Added 2026-09-13. Additive, opt-in, off by default.

## The URL

    https://api.owen.santiagoproperties.uk/webhooks/openphone

`POST` only. Traefik already routes every path on `api.${APP_DOMAIN}` to `callmon_app:8888`,
so no Traefik or compose change is needed.

## What the owner does in the Quo dashboard

OWEN never registers the webhook itself — registering one is a write to Quo, and OWEN only
ever reads from Quo. So this part is done by hand, once:

1. In Quo, open **Settings → Webhooks** and click **Create webhook**.
2. **URL:** `https://api.owen.santiagoproperties.uk/webhooks/openphone`
3. **Events** — tick exactly these six:
   - `message.received`
   - `message.delivered`  (a text somebody sends from the Quo app appears on the CRM thread)
   - `call.completed`
   - `call.recording.completed`
   - `call.transcript.completed`
   - `call.summary.completed`

   Anything else you tick (`call.ringing`, `contact.*`, `task.*`) is acknowledged with 200
   and ignored.
4. **Resources / phone numbers:** select the Quo business line (the one ending 7244).
5. Optionally label it `OWEN → CRM`, then **Save**.
6. On the new webhook's details page click **⋯ → Reveal signing secret** and copy the
   base64 value.
7. On the server, in `/opt/santiagoproperties/.../.env.prod` for owen-main (the operator
   does this and redeploys the app container):

       OPENPHONE_WEBHOOK_SECRET=<the value from step 6>
       OPENPHONE_WEBHOOK_ENABLED=true
       # the webhook reuses the mirror, so these must already be set:
       OPENPHONE_MIRROR_ENABLED=true
       OPENPHONE_API_KEY=<the existing Quo API key>
       AGENT_RUNTIME_KEY=<already set on a working deploy>

   The secret is never logged and never returned in a response. Keep it out of chat and
   tickets like any other credential.

Order matters only in one way: until step 7 is deployed the route answers **404**, and Quo
will show failed deliveries. That is harmless — the 5-minute poll keeps mirroring — and
they stop the moment the switch is on.

## How a delivery is verified

Quo's documented scheme (support.quo.com, "Webhooks"):

    openphone-signature: hmac;1;<timestamp ms>;<base64 digest>
    digest = base64(HMAC-SHA256(base64decode(secret), <timestamp> + "." + <body>))

* compared with `hmac.compare_digest`;
* `<body>` is the raw request body; the compact JSON re-serialisation (Quo's Node sample,
  `JSON.stringify(req.body)`) is also accepted — both need the key;
* a timestamp more than `OPENPHONE_WEBHOOK_TOLERANCE_SECONDS` (300) from now, either way,
  is refused as a replay;
* missing / malformed / wrong / stale → **401**, nothing recorded, nothing queued.

## What happens after 200

The receiver writes ONE `crm_report` job and answers. The worker posts it back to OWEN's
own `POST /api/openphone-mirror/webhook-events` (scope `agent_write`), which runs the
poll's own `sync._mirror_message` / `sync._mirror_call`. `openphone_mirror_rows UNIQUE
(kind, external_id)` and the CRM's unique `dedupe_key` make a webhook and the poll produce
one CRM event for one object, in either order. A call's recording, transcript and summary
are each sent once more under the call's own dedupe key and fill the one CRM row.

Guards, in order: switch off → 404 · no secret → 503 · bad signature → 401 · unhandled
type → 200 ignored · mirror not ready → 503 · else queue → 200.

## Not verified without live credentials

* No real Quo delivery has reached this code. The scheme matches the published docs and
  both published samples; the first real delivery is the proof.
* Whether `GET /call-recordings/{id}` also serves a VOICEMAIL (the webhook marks an
  unanswered call with a voicemail as having audio) is unverified; if not, that row's
  player answers 404 and the rest of the row is intact.
* `GET /calls/{id}` (used when a transcript/summary event names a call the mirror has not
  seen yet) is documented and on the read-only client, but was not in the 2026-07-24 probe.
