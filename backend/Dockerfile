# syntax=docker/dockerfile:1.7
#
# QuakeAngel FastAPI backend — production container.
#
# Design intent (locked with Paul during Fly.io migration prep, 2026-08-06):
#
#   1. **Single stage, minimal image.** The FastAPI service is small and
#      the app is safety-critical — a compact image (~200MB) means faster
#      cold-starts if Fly.io ever needs to move the machine between hosts
#      during a rolling deploy. Slim base + no dev tooling.
#
#   2. **Non-root user.** APNs .p8 keys live inside the container's
#      Mongo (encrypted at rest) but the process itself should not run
#      as root — defense-in-depth against any future dependency CVE.
#
#   3. **In-container health check.** `/api/status` is our liveness
#      probe. Fly.io reads this to know when the machine is ready to
#      accept traffic during a deploy. Timeout is generous (15s) because
#      the EMSC poller can be mid-request when we probe.
#
#   4. **No layer for dev dependencies.** requirements.txt is what
#      ships. If we later want a dev-container we'll add a separate
#      Dockerfile.dev with pytest/black/etc.
#
#   5. **Uvicorn with --workers=2** on Fly.io's shared-cpu-1x (256MB).
#      One worker for the HTTP request loop, one for the background
#      EMSC poller. If you scale up to shared-cpu-2x, bump to 4.
#
#      NOTE: the EMSC poller runs as an asyncio task inside the
#      FastAPI process (server.py:startup), not a separate service.
#      Do NOT set `--workers > 1` if we ever refactor the poller to
#      hold in-process global state — right now everything the poller
#      needs is in Mongo, so multiple workers are safe but they will
#      each try to poll (wasted API calls to EMSC). If cost matters,
#      keep workers=1 and increase Fly.io machine tier for burst.

FROM python:3.11-slim-bookworm

# Environment hygiene — no .pyc files, unbuffered logs so Fly.io's
# log viewer shows output immediately (not after buffer flush).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimum system deps: curl for healthcheck, ca-certificates for TLS
# to EMSC/USGS/Apple/Google. Everything else stays out of the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. UID 1000 is conventional; matches Fly.io's expectations.
RUN groupadd --system --gid 1000 appuser \
    && useradd --system --uid 1000 --gid appuser --home /app appuser

WORKDIR /app

# Copy requirements first for better Docker layer caching — this layer
# only invalidates when requirements.txt changes, not on every code edit.
COPY --chown=appuser:appuser backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the application code.
COPY --chown=appuser:appuser backend /app

USER appuser

# Fly.io routes traffic to whatever port the process listens on; the
# convention is $PORT env var. Default to 8001 to match our local dev
# setup so a `docker run` without Fly.io still Just Works.
ENV PORT=8001
EXPOSE 8001

# Health check — Fly.io's own health check will hit /api/status via
# fly.toml [[http_service.checks]], but keeping a Docker HEALTHCHECK
# here means `docker ps` shows meaningful state for local debugging too.
HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error --max-time 10 \
        "http://127.0.0.1:${PORT}/api/status" || exit 1

# Single worker default. See design note #5. Override via CMD if
# scaling up to a bigger Fly.io tier.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info --no-server-header --proxy-headers --forwarded-allow-ips='*'"]
