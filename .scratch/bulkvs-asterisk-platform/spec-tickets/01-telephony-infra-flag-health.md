# 01 — Telephony infra + ASTERISK_ENABLED flag + /health/telephony

**What to build:** Native Asterisk deploys dark and flag-gated: the operator-owner can flip ASTERISK_ENABLED on and see telephony come alive (ARI reachable from the backend, trunk registered) without any existing behavior changing, and flip it off to fully revert.

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] `asterisk/` config lives in-repo; deploy is rsync + targeted reload (never restart)
- [ ] `ASTERISK_ENABLED` defaults off; with it off nothing about the existing app changes
- [ ] Backend reaches ARI at loopback/host-gateway via `host.docker.internal`, UFW-allowed only from the pinned `callmon-net` subnet; creds from `.env.prod`
- [ ] SIP `5060/udp` IP-locked to the 4 BulkVS SBC IPs; RTP `10000-10200/udp` open-but-session-validated
- [ ] Asterisk runs as a systemd service (`Restart=always`, version pinned 22.10.1), decoupled from `make deploy`
- [ ] A non-gating `/health/telephony` endpoint reports trunk + ARI-WS status; the deploy healthcheck stays app-only
