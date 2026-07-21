# Telephony infra — security + deployment of native Asterisk

Type: grilling
Status: open
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
