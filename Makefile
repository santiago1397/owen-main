SSH_ALIAS ?= owen-main
VPS_REPO_PATH ?= /opt/santiagoproperties/owen-main
COMPOSE = docker compose -f docker-compose.prod.yml --env-file .env.prod

.PHONY: help build up down logs db-revision db-upgrade create-admin deploy check test issue-key ai-smoke smoke-routes

help:
	@echo "make build         Build images"
	@echo "make up             Start stack (app + worker)"
	@echo "make down           Stop stack"
	@echo "make logs           Tail logs"
	@echo "make db-revision m='msg'   Autogenerate an Alembic migration (backend/)"
	@echo "make db-upgrade     Apply migrations to head (backend/)"
	@echo "make create-admin e=email p=pass   Create/reset the admin user"
	@echo "make issue-key n=name s='read logs'   Mint an AI API key (shown once; UI: /api-keys)"
	@echo "make ai-smoke       Cross-check the AI API against the dashboard (needs env vars)"
	@echo "make check          Lint + every test + typecheck. Runs automatically before deploy."
	@echo "make smoke-routes   Hit every JWT-authed GET on the LIVE server (post-deploy)"
	@echo "make deploy         SSH to VPS, git pull --ff-only, rebuild, restart, healthcheck"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

# Local dev migration helpers (run from backend/, needs a reachable Postgres).
db-revision:
	cd backend && alembic revision --autogenerate -m "$(m)"

db-upgrade:
	cd backend && alembic upgrade head

create-admin:
	$(COMPOSE) exec app python -m app.scripts.create_admin "$(e)" "$(p)"

# Passthrough to the admin CLI, e.g.: make manage args='add-number --phone +1... --campaign "CL Ads 2"'
manage:
	$(COMPOSE) exec app python -m app.scripts.manage $(args)

# Mint an AI API key from the shell. The UI (/api-keys) is the normal route; this exists for
# bootstrapping a fresh deployment, e.g.:
#   make issue-key n=claude-cli s='read content sql logs'
issue-key:
	$(COMPOSE) exec app python -m app.scripts.manage issue-key --name "$(n)" \
		$(foreach scope,$(s),--scope $(scope))

# Live cross-check that /api/ai/calls/stats agrees with /api/dashboard/summary.
# Needs OWEN_API_URL, OWEN_API_KEY, and (for the cross-check) OWEN_EMAIL + OWEN_PASSWORD.
ai-smoke:
	cd backend && python -m tests.smoke_ai_api

# The pre-deploy gate: pyflakes + every backend/owen-voice test + tsc. Offline, no DB.
# `make deploy` runs it first; SKIP_CHECKS=1 make deploy bypasses it for an emergency.
check:
	@bash scripts/check.sh

# Alias, because `test` was listed in .PHONY for months with no target behind it — which is
# exactly how a suite stops being run.
test: check

# Post-deploy: log in and GET every JWT-authed read endpoint against the running container.
# This is the check that would have caught the /api/calls/{id} 500 on the day it shipped.
smoke-routes:
	$(COMPOSE) exec -T app python -m tests.smoke_routes

deploy:
	@SSH_ALIAS="$(SSH_ALIAS)" VPS_REPO_PATH="$(VPS_REPO_PATH)" bash scripts/deploy.sh
