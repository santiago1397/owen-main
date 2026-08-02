SSH_ALIAS ?= callmon
VPS_REPO_PATH ?= /opt/santiagoproperties/owen-main
COMPOSE = docker compose -f docker-compose.prod.yml --env-file .env.prod

.PHONY: help build up down logs db-revision db-upgrade create-admin deploy test issue-key ai-smoke

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

deploy:
	@SSH_ALIAS="$(SSH_ALIAS)" VPS_REPO_PATH="$(VPS_REPO_PATH)" bash scripts/deploy.sh
