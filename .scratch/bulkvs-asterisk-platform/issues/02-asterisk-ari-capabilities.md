# Asterisk / ARI capabilities + BulkVS trunk config

Type: research
Status: open
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

<!-- resolved by /research subagent; link the captured research file here -->
