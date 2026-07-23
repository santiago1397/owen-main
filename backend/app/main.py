import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import agents as agents_api
from app.api import auth
from app.api import callers as callers_api
from app.api import calls as calls_api
from app.api import campaigns as campaigns_api
from app.api import dashboard as dashboard_api
from app.api import emails as emails_api
from app.api import flows as flows_api
from app.api import health as health_api
from app.api import numbers as numbers_api
from app.api import recordings as recordings_api
from app.api import settings as settings_api
from app.core.config import settings
from app.db import engine
from app.migrate import run_migrations
from app.api import messages as messages_api
from app.webhooks import bulkvs as bulkvs_webhooks
from app.webhooks import signalwire as signalwire_webhooks
from app.webhooks import twilio as twilio_webhooks

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Both app and worker migrate at startup behind an advisory lock (one wins).
    run_migrations()
    yield


app = FastAPI(title="Call Monitoring Platform", lifespan=lifespan)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth.router)
app.include_router(calls_api.router)
app.include_router(numbers_api.router)
app.include_router(callers_api.router)
app.include_router(campaigns_api.router)
app.include_router(dashboard_api.router)
app.include_router(emails_api.router)
app.include_router(flows_api.router)
app.include_router(agents_api.router)
app.include_router(recordings_api.router)
app.include_router(settings_api.router)
app.include_router(messages_api.router)
app.include_router(health_api.router)
app.include_router(twilio_webhooks.router)
app.include_router(signalwire_webhooks.router)
app.include_router(bulkvs_webhooks.router)


@app.get("/health")
async def health() -> dict:
    # Verify DB reachability so the container healthcheck fails if Postgres is down.
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "env": settings.ENVIRONMENT}
