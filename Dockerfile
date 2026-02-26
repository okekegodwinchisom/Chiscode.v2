# ╔══════════════════════════════════════════════════════════════╗
# ║  ChisCode — Hugging Face Spaces Dockerfile                  ║
# ║  Target: HF Docker Space                                    ║
# ║  Port: 7860 (HF requirement)                                ║
# ║  User: UID 1000 (HF non-root requirement)                   ║
  # ║  Build: -stage  |  Package manager: uv                 ║
# ╚══════════════════════════════════════════════════════════════╝

# ── Stage 1: Builder ─────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Pull uv from its official image — fastest Python package installer
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Build-time system deps only (not carried into runtime image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy manifest first — layer cache: dep changes ≠ code changes
COPY pyproject.toml .

# Install runtime deps only into an isolated venv
# --no-dev   → skip ruff, pytest, mypy
# --no-cache → no uv cache dir left behind in the image layer
# --frozen   → fail fast if lockfile is stale (run `uv lock` locally first)
RUN uv venv .venv && \
    uv sync --no-dev --no-cache

# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="ChisCode Team"
LABEL description="ChisCode AI Agent Builder — FastAPI on Hugging Face Spaces"
LABEL org.opencontainers.image.source="https://github.com/your-org/chiscode"

# ── Runtime system deps ───────────────────────────────────────────
# curl  → HEALTHCHECK
# git   → GitHub service creates/clones repos at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# ── HF Spaces user requirement ────────────────────────────────────
# Hugging Face Spaces runs containers as a non-root user with UID 1000.
# The user MUST exist in the image with exactly UID=1000, GID=1000.
# If you use a different UID the Space will refuse to start.
RUN groupadd --gid 1000 chiscode \
    && useradd --uid 1000 \
               --gid 1000 \
               --home-dir /home/chiscode \
               --shell /bin/bash \
               --create-home \
               chiscode

WORKDIR /app

# Copy pre-built venv from builder (no pip/uv needed in this stage)
COPY --from=builder /app/.venv /app/.venv

# Make venv binaries available on PATH
ENV PATH="/app/.venv/bin:$PATH"

# Copy application source — chown everything to UID 1000 at copy time
# (cheaper than a separate chown RUN layer)
COPY --chown=1000:1000 ./backend/app       ./app
COPY --chown=1000:1000 ./frontend          ./frontend

# Runtime directories the app writes to
RUN mkdir -p /app/logs /app/temp \
    && chown -R 1000:1000 /app

# ── Python runtime flags ──────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

# ── App defaults ──────────────────────────────────────────────────
# APP_ENV and all secrets are set as HF Space Secrets in the UI —
# never bake real values into the image.
ENV APP_ENV=production \
    PORT=7860

# ── HF Spaces port ───────────────────────────────────────────────
# Hugging Face Spaces REQUIRES port 7860.
# The platform reverse-proxies public traffic to this port.
# Do NOT use 8000 — the Space will appear offline.
EXPOSE 7860

# Drop privileges before process starts
USER 1000

# ── Health check ──────────────────────────────────────────────────
# start-period gives time for MongoDB/Redis connections on cold boot
HEALTHCHECK --interval=30s \
            --timeout=10s \
            --start-period=60s \
            --retries=3 \
    CMD curl -f http://localhost:7860/health || exit 1

# ── Entrypoint ────────────────────────────────────────────────────
# uvloop  → fastest async event loop for Python
# h11     → fastest pure-Python HTTP/1.1 parser
# 2 workers is right for HF Spaces CPU instances (2 vCPU)
# Upgrade to 4 workers if on a GPU/larger instance
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "7860", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--http", "h11", \
     "--no-access-log"]
     