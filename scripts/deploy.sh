#!/usr/bin/env bash
# Deploy: SSH to the VPS, fast-forward pull from GitHub, rebuild, restart, healthcheck.
# Mirrors the reference flow in ../../santiago/SERVER_SETUP.md.
set -euo pipefail

SSH_ALIAS="${SSH_ALIAS:-callmon}"
VPS_REPO_PATH="${VPS_REPO_PATH:-/opt/santiagoproperties/owen-main}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-http://localhost:8888/health}"
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"

# Extra ssh flags. Deploying from Git Bash on Windows needs SSH_OPTS='-o ControlPath=none':
# connection multiplexing (ControlMaster/ControlPath in ~/.ssh/config) cannot work over the
# MSYS socket emulation, and every ssh call drowns in "mux_client_request_session: read from
# master failed". `ControlMaster=no` alone is NOT enough — ssh still tries to reuse the
# existing ControlPath socket; only ControlPath=none turns multiplexing off entirely.
# Empty by default so Linux/macOS deploys keep multiplexing and stay fast.
SSH_OPTS="${SSH_OPTS:-}"
ssh_() { ssh ${SSH_OPTS} "$@"; }

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
for i in $(seq 1 30); do
  if ssh_ "${SSH_ALIAS}" "curl -fsS ${HEALTHCHECK_URL} >/dev/null 2>&1"; then
    echo "==> Healthy. Deploy complete."
    exit 0
  fi
  sleep 3
done

echo "ERROR: backend did not become healthy in time. Last logs:"
ssh_ "${SSH_ALIAS}" "cd ${VPS_REPO_PATH} && ${COMPOSE} logs --tail=80 app"
exit 1
