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
| `pjsip.conf` | BulkVS trunk: transport, endpoint, auth, aor, IP `identify`, optional registration |
| `ari.conf` | ARI user the backend authenticates as |
| `http.conf` | HTTP server hosting ARI + the ARI WebSocket (bind + port) |
| `rtp.conf` | RTP media port range (10000-10200) |
| `extensions.conf` | Minimal dialplan: inbound → Stasis(`${ARI_APP}`), outbound → trunk |

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
```

The backend reads all of these except `ARI_BIND_ADDR` (that one is only used when
rendering `http.conf` on the host).

## Deploy: render + rsync + targeted reload

1. **Install & pin Asterisk 22.10.1** on the host, then freeze it so it never
   auto-upgrades under you:
   ```
   apt-mark hold asterisk
   ```

2. **Render** the templates from `.env.prod` and **rsync** into `/etc/asterisk/`:
   ```bash
   set -a; . /opt/santiagoproperties/owen-main/.env.prod; set +a
   for f in pjsip ari http rtp extensions; do
     envsubst < asterisk/$f.conf > /tmp/$f.conf
   done
   rsync -a /tmp/{pjsip,ari,http,rtp,extensions}.conf /etc/asterisk/
   ```

3. **Targeted reload — never restart** (a restart drops in-flight calls):
   ```bash
   asterisk -rx "pjsip reload"
   asterisk -rx "dialplan reload"
   asterisk -rx "module reload res_ari.so res_http_websocket.so"
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

```bash
for ip in 162.249.171.198 76.8.29.198 69.12.88.198 199.255.157.198; do
  ufw allow from $ip to any port 5060 proto udp
done
ufw allow 10000:10200/udp
ufw allow from <callmon-net subnet, e.g. 172.20.0.0/16> to any port 8088 proto tcp
```

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
