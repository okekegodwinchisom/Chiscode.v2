# ╔══════════════════════════════════════════════════════════════╗
# ║  ChisCode — Hugging Face Spaces Dockerfile                  ║
# ║  Port: 7860  |  User: UID 1000  |  Build: multi-stage       ║
# ╚══════════════════════════════════════════════════════════════╝
#
# REQUIRED repo layout (paths are relative to repo root):
#
#   your-repo/
#   ├── Dockerfile          ← this file (MUST be at repo root)
#   ├── pyproject.toml      ← dependency manifest
#   ├── backend/
#   │   └── app/            ← FastAPI source code
#   └── frontend/           ← templates + static assets
#       ├── templates/
#       └── static/
#
# HF Spaces builds from repo root as the Docker build context.
# Every COPY path is relative to that root.

# ── Stage 1: Builder ─────────────────────────────────────────────
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy manifest from repo root — layer cache: only reinstalls when
# pyproject.toml changes, not on every code change
COPY pyproject.toml .

RUN uv venv .venv && \
    uv sync --no-group dev --no-cache

# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="ChisCode Team"
LABEL description="ChisCode AI Agent Builder — FastAPI on Hugging Face Spaces"

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# HF Spaces requires UID/GID exactly 1000
RUN groupadd --gid 1000 chiscode \
    && useradd  --uid 1000 \
                --gid 1000 \
                --home-dir /home/chiscode \
                --shell /bin/bash \
                --create-home \
                chiscode

WORKDIR /app

# Copy pre-built venv from builder stage
COPY --from=builder /build/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

# ── Copy application source ───────────────────────────────────────
# backend/app/ → /app/app/
# The COPY source path is relative to the repo root (build context).
COPY --chown=1000:1000 backend/app ./app

# frontend/ → /app/frontend/
# This directory MUST exist at the repo root.
# If missing, the build will fail with: "/frontend": not found
COPY --chown=1000:1000 frontend ./frontend

# Runtime writable directories
RUN mkdir -p /app/logs /app/temp \
    && chown -R 1000:1000 /app/logs /app/temp

# ── Environment ───────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    APP_ENV=production \
    PORT=7860

# All secrets (API keys, DB URLs, JWT keys) are set via:
# HF Space → Settings → Variables and Secrets
# They are injected at runtime — never bake them into the image.

USER 1000

EXPOSE 7860

HEALTHCHECK --interval=30s \
            --timeout=10s \
            --start-period=60s \
            --retries=3 \
    CMD curl -f http://localhost:7860/health || exit 1

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "7860", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "h11", \
     "--no-access-log"]