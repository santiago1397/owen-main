#!/usr/bin/env bash
# Deploy: SSH to the VPS, fast-forward pull from GitHub, rebuild, restart, healthcheck.
# Mirrors the reference flow in ../../santiago/SERVER_SETUP.md.
set -euo pipefail

SSH_ALIAS="${SSH_ALIAS:-callmon}"
VPS_REPO_PATH="${VPS_REPO_PATH:-/opt/owen/callmon}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://localhost:8888/health}"
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"

echo "==> Checking SSH alias '${SSH_ALIAS}'"
ssh -o BatchMode=yes "${SSH_ALIAS}" true

echo "==> Verifying .env.prod exists on the server"
ssh "${SSH_ALIAS}" "test -f ${VPS_REPO_PATH}/.env.prod" \
  || { echo "ERROR: ${VPS_REPO_PATH}/.env.prod missing on server"; exit 1; }

echo "==> Pull (ff-only), build, up"
ssh "${SSH_ALIAS}" "cd ${VPS_REPO_PATH} \
  && git fetch origin \
  && git merge --ff-only origin/main \
  && ${COMPOSE} build \
  && ${COMPOSE} up -d"

echo "==> Waiting for healthcheck"
for i in $(seq 1 30); do
  if ssh "${SSH_ALIAS}" "curl -fsS ${HEALTHCHECK_URL} >/dev/null 2>&1"; then
    echo "==> Healthy. Deploy complete."
    exit 0
  fi
  sleep 3
done

echo "ERROR: backend did not become healthy in time. Last logs:"
ssh "${SSH_ALIAS}" "cd ${VPS_REPO_PATH} && ${COMPOSE} logs --tail=80 app"
exit 1
