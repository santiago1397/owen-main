# owen-voice — AudioSocket media service (AI_AGENT_SPEC D2).
# Mirrors the backend image conventions: slim base, non-root, no build toolchain in the
# final layer.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# curl is here only for the container healthcheck (same pattern as the backend image).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as non-root (SERVER_SETUP.md security checklist). Same pattern and same name as
# backend/Dockerfile deliberately: `voice` cannot be used here because Debian's base image
# already ships a system GROUP called `voice`, so `useradd voice` fails trying to create it.
RUN useradd --create-home appuser && chown -R appuser /srv
USER appuser

# 8099 = control API, 9092 = AudioSocket listener.
EXPOSE 8099 9092

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
