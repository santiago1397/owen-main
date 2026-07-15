FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as non-root (SERVER_SETUP.md security checklist).
RUN useradd --create-home appuser && mkdir -p /data/recordings && chown -R appuser /data
USER appuser

EXPOSE 8888

# Default command is the API; the worker service overrides `command:` in compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888", "--workers", "4"]
