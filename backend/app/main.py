"""
ChisCode — FastAPI Application Entry Point
==========================================
App factory with:
  - Lifespan management (MongoDB + Redis connect/disconnect)
  - Security middleware (CORS, TrustedHost, security headers)
  - GZip compression
  - Request timing + structured logging
  - Static files + Jinja2 templates
  - Health endpoints (/health, /health/detailed)
  - Frontend page routes
  - Global exception handlers

HF Spaces: runs on port 7860, UID 1000, production env.
"""

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db import mongodb, redis_client

# ── Bootstrap logging before everything else ─────────────────────
# Ensures any startup errors are captured in structured format.
setup_logging()
logger = get_logger(__name__)

# ── Path resolution ───────────────────────────────────────────────
# Works correctly whether running locally or inside the HF Spaces
# Docker container (where /app/app/ and /app/frontend/ are mounted).
#
# Container layout (from Dockerfile COPY commands):
#   /app/app/        ← backend source  (this file lives here)
#   /app/frontend/   ← templates + static
#
# Local layout:
#   backend/app/     ← this file
#   frontend/        ← two levels up from this file

_THIS_FILE   = Path(__file__).resolve()          # .../app/main.py
_APP_DIR     = _THIS_FILE.parent                 # .../app/
_BACKEND_DIR = _APP_DIR.parent                   # .../backend/  (or /app/)

# Walk up until we find the frontend/ sibling directory.
# This makes the path resolution environment-agnostic.
def _find_frontend() -> Path:
    for candidate in [_BACKEND_DIR, _BACKEND_DIR.parent]:
        p = candidate / "frontend"
        if p.exists():
            return p
    # Fallback: HF Spaces container root
    fallback = Path("/app/frontend")
    return fallback

FRONTEND_DIR   = _find_frontend()
STATIC_DIR     = FRONTEND_DIR / "static"
TEMPLATES_DIR  = FRONTEND_DIR / "templates"


# ── Lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Async context manager for application startup and shutdown.
    FastAPI calls this once — yield separates startup from shutdown.
    Any exception raised before yield will abort startup cleanly.
    """
    logger.info(
        "ChisCode starting up",
        env=settings.app_env,
        version=settings.app_version,
        port=settings.port,
        debug=settings.debug,
        frontend_dir=str(FRONTEND_DIR),
    )

    # ── Startup ───────────────────────────────────────────────────
    startup_errors: list[str] = []

    try:
        await mongodb.connect()
    except Exception as exc:
        # Log but don't crash — allows health/detailed to report degraded
        startup_errors.append(f"MongoDB: {exc}")
        logger.error("MongoDB connection failed at startup", error=str(exc))

    try:
        await redis_client.connect()
    except Exception as exc:
        startup_errors.append(f"Redis: {exc}")
        logger.error("Redis connection failed at startup", error=str(exc))

    if startup_errors:
        logger.warning(
            "ChisCode started with degraded connections",
            errors=startup_errors,
        )
    else:
        logger.info("All connections established — ChisCode is ready")

    # ── Hand off to application ───────────────────────────────────
    yield

    # ── Shutdown ──────────────────────────────────────────────────
    logger.info("ChisCode shutting down")
    await mongodb.disconnect()
    await redis_client.disconnect()
    logger.info("Shutdown complete")


# ── App Factory ───────────────────────────────────────────────────

def create_app() -> FastAPI:
    """
    Build and configure the FastAPI application.
    Called once at module load — returns the ASGI app instance.
    """

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "AI-powered agent builder — "
            "natural language to production-ready web applications."
        ),
        # Disable interactive docs in production (security best practice).
        # Set DEBUG=true in .env to re-enable during development.
        docs_url="/docs"        if settings.debug else None,
        redoc_url="/redoc"      if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=lifespan,
    )

    # ── Middleware stack ──────────────────────────────────────────
    # Order matters — FastAPI applies middleware bottom-up (last added = outermost).

    # 1. GZip — compress responses > 1KB (saves bandwidth on HF Spaces)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # 2. Trusted host — reject requests with unexpected Host headers
    #    Prevents host header injection attacks.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts,
    )

    # 3. CORS — allow the frontend origin to make credentialed requests.
    #    On HF Spaces this is the Space's public URL.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_base_url],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Process-Time", "X-Toast-Message", "X-Toast-Type"],
    )

    # 4. Security headers + request timing (custom middleware)
    @app.middleware("http")
    async def request_middleware(request: Request, call_next):
        # Bind request context to every log line emitted during this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            method=request.method,
            path=request.url.path,
            client=request.client.host if request.client else "unknown",
        )

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        # Timing header — useful for frontend performance debugging
        response.headers["X-Process-Time"] = f"{duration_ms}ms"

        # Security headers — applied to every response
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        logger.info(
            "Request",
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    # ── Static files ──────────────────────────────────────────────
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        logger.debug("Static files mounted", path=str(STATIC_DIR))
    else:
        logger.warning("Static directory not found — UI assets unavailable", path=str(STATIC_DIR))

    # ── Jinja2 templates ──────────────────────────────────────────
    templates: Jinja2Templates | None = None
    if TEMPLATES_DIR.exists():
        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        # Inject globals available in every template
        templates.env.globals.update({
            "app_name":    settings.app_name,
            "app_version": settings.app_version,
            "app_env":     settings.app_env,
            "debug":       settings.debug,
        })
        logger.debug("Templates loaded", path=str(TEMPLATES_DIR))
    else:
        logger.warning("Templates directory not found — HTML routes unavailable", path=str(TEMPLATES_DIR))

    # ── API routes ────────────────────────────────────────────────
    app.include_router(api_router, prefix="/api/v1")

    # ── System endpoints ──────────────────────────────────────────

    @app.get("/health", tags=["system"], include_in_schema=True)
    async def health():
        """
        Shallow health check — always returns 200 if the process is alive.
        Used by the Dockerfile HEALTHCHECK and HF Spaces uptime monitoring.
        """
        return {
            "status":  "ok",
            "app":     settings.app_name,
            "version": settings.app_version,
            "env":     settings.app_env,
        }

    @app.get("/health/detailed", tags=["system"], include_in_schema=True)
    async def health_detailed():
        """
        Deep health check — verifies live connectivity to MongoDB and Redis.
        Returns 200 if all checks pass, 503 if any are degraded.
        Safe to call repeatedly — read-only ping commands only.
        """
        checks: dict[str, str] = {}

        try:
            await mongodb.get_db().command("ping")
            checks["mongodb"] = "ok"
        except Exception as exc:
            checks["mongodb"] = f"error: {exc}"

        try:
            await redis_client.get_redis().ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {exc}"

        all_ok = all(v == "ok" for v in checks.values())
        http_status = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE

        return JSONResponse(
            status_code=http_status,
            content={
                "status": "ok" if all_ok else "degraded",
                "checks": checks,
            },
        )

    # ── Frontend page routes ──────────────────────────────────────
    # Only registered when Jinja2 templates are available.
    # Each route passes `request` (required by Jinja2) and any
    # page-specific context the template needs.

    if templates:

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def index(request: Request):
            return templates.TemplateResponse(
                "index.html",
                {"request": request},
            )

        @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
        async def login_page(request: Request):
            return templates.TemplateResponse(
                "auth/login.html",
                {"request": request},
            )

        @app.get("/register", response_class=HTMLResponse, include_in_schema=False)
        async def register_page(request: Request):
            return templates.TemplateResponse(
                "auth/register.html",
                {"request": request},
            )

        @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
        async def dashboard(request: Request):
            return templates.TemplateResponse(
                "dashboard/index.html",
                {"request": request},
            )

        @app.get("/pricing", response_class=HTMLResponse, include_in_schema=False)
        async def pricing(request: Request):
            return templates.TemplateResponse(
                "pricing.html",
                {"request": request},
            )

    # ── Exception handlers ────────────────────────────────────────

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc):
        # API calls always get JSON
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=404,
                content={"detail": "Endpoint not found."},
            )
        # Browser navigation: serve the 404 HTML page if available
        if templates:
            try:
                return templates.TemplateResponse(
                    "404.html",
                    {"request": request},
                    status_code=404,
                )
            except Exception:
                pass
        return JSONResponse(
            status_code=404,
            content={"detail": "Not found."},
        )

    @app.exception_handler(405)
    async def method_not_allowed_handler(request: Request, exc):
        return JSONResponse(
            status_code=405,
            content={"detail": f"Method {request.method} not allowed."},
        )

    @app.exception_handler(422)
    async def validation_error_handler(request: Request, exc):
        """
        Return validation errors in a consistent, client-friendly shape.
        FastAPI's default 422 body is verbose — this simplifies it.
        """
        errors = []
        if hasattr(exc, "errors"):
            for err in exc.errors():
                field = " → ".join(str(loc) for loc in err.get("loc", []))
                errors.append({"field": field, "message": err.get("msg", "")})
        return JSONResponse(
            status_code=422,
            content={"detail": "Validation error.", "errors": errors},
        )

    @app.exception_handler(500)
    async def internal_error_handler(request: Request, exc):
        logger.error(
            "Unhandled server error",
            path=request.url.path,
            method=request.method,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. The team has been notified."},
        )

    return app


# ── Application instance ──────────────────────────────────────────
# Uvicorn imports this directly: uvicorn app.main:app
app = create_app()
