#!/usr/bin/env bash
# Deploy: SSH to the VPS, fast-forward pull from GitHub, rebuild, restart, healthcheck.
# Mirrors the reference flow in ../../santiago/SERVER_SETUP.md.
set -euo pipefail

SSH_ALIAS="${SSH_ALIAS:-owen-main}"
VPS_REPO_PATH="${VPS_REPO_PATH:-/opt/santiagoproperties/owen-main}"
# Probed from INSIDE the app container, because the app is `expose:`-only — nothing has
# ever listened on the host's :8888, so the old host-side `curl localhost:8888` could not
# succeed and every deploy ended with a false "did not become healthy" after 90 seconds of
# waiting. A check that cannot pass is worse than no check: it trains you to ignore it.
HEALTHCHECK_CMD="${HEALTHCHECK_CMD:-docker exec callmon_app curl -fsS http://localhost:8888/health}"
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"

# Extra ssh flags. Deploying from Git Bash on Windows needs SSH_OPTS='-o ControlPath=none':
# connection multiplexing (ControlMaster/ControlPath in ~/.ssh/config) cannot work over the
# MSYS socket emulation, and every ssh call drowns in "mux_client_request_session: read from
# master failed". `ControlMaster=no` alone is NOT enough — ssh still tries to reuse the
# existing ControlPath socket; only ControlPath=none turns multiplexing off entirely.
# Empty by default so Linux/macOS deploys keep multiplexing and stay fast.
SSH_OPTS="${SSH_OPTS:-}"
ssh_() { ssh ${SSH_OPTS} "$@"; }

# Gate FIRST, before anything touches the server. Three bugs reached production in the agent
# build because code was written and never executed once; scripts/check.sh finds that class
# offline in seconds. SKIP_CHECKS=1 exists for an emergency rollback, not for convenience.
if [ "${SKIP_CHECKS:-0}" != "1" ]; then
  echo "==> Pre-deploy checks"
  bash "$(dirname "$0")/check.sh" || { echo "ERROR: checks failed; refusing to deploy"; exit 1; }
else
  echo "==> Pre-deploy checks SKIPPED (SKIP_CHECKS=1)"
fi

echo "==> Checking SSH alias '${SSH_ALIAS}'"
ssh_ -o BatchMode=yes "${SSH_ALIAS}" true

echo "==> Verifying .env.prod exists on the server"
ssh_ "${SSH_ALIAS}" "test -f ${VPS_REPO_PATH}/.env.prod" \
  || { echo "ERROR: ${VPS_REPO_PATH}/.env.prod missing on server"; exit 1; }

echo "==> Pull (ff-only), build, up"
ssh_ "${SSH_ALIAS}" "cd ${VPS_REPO_PATH} \
  && git fetch origin \
  && git merge --ff-only origin/main \
  && ${COMPOSE} build \
  && ${COMPOSE} up -d"

echo "==> Waiting for healthcheck"
healthy=0
for i in $(seq 1 30); do
  if ssh_ "${SSH_ALIAS}" "${HEALTHCHECK_CMD} >/dev/null 2>&1"; then
    healthy=1
    break
  fi
  sleep 3
done

if [ "${healthy}" -ne 1 ]; then
  echo "ERROR: backend did not become healthy in time. Last logs:"
  ssh_ "${SSH_ALIAS}" "cd ${VPS_REPO_PATH} && ${COMPOSE} logs --tail=80 app"
  exit 1
fi
echo "==> Healthy."

# Post-deploy: actually REQUEST the surface the UI uses. /health only proves the process is
# up and Postgres answers SELECT 1 — it says nothing about whether an endpoint 500s, which
# is precisely how a broken /api/calls/{id} survived five days and two deploys.
if [ "${SKIP_SMOKE:-0}" != "1" ]; then
  echo "==> Route smoke (every JWT-authed GET)"
  ssh_ "${SSH_ALIAS}" "cd ${VPS_REPO_PATH} && ${COMPOSE} exec -T app python -m tests.smoke_routes"     || { echo "ERROR: routes are answering 5xx on the freshly deployed build (see above)"; exit 1; }
fi

echo "==> Deploy complete."
