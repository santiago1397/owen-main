SSH_ALIAS ?= callmon
VPS_REPO_PATH ?= /opt/santiagoproperties/owen-main
COMPOSE = docker compose -f docker-compose.prod.yml --env-file .env.prod

.PHONY: help build up down logs db-revision db-upgrade create-admin deploy

help:
	@echo "make build         Build images"
	@echo "make up             Start stack (app + worker)"
	@echo "make down           Stop stack"
	@echo "make logs           Tail logs"
	@echo "make db-revision m='msg'   Autogenerate an Alembic migration (backend/)"
	@echo "make db-upgrade     Apply migrations to head (backend/)"
	@echo "make create-admin e=email p=pass   Create/reset the admin user"
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

deploy:
	@SSH_ALIAS="$(SSH_ALIAS)" VPS_REPO_PATH="$(VPS_REPO_PATH)" bash scripts/deploy.sh
