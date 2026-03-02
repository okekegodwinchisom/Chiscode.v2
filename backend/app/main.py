"""
ChisCode — FastAPI Application
Main app factory with lifespan management, middleware, and routing.
"""
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.router import router as api_router  # Import router as api_router
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db import mongodb, redis_client

# Initialise logging before anything else
setup_logging()
logger = get_logger(__name__)


# ── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of all connections."""
    logger.info(
        "ChisCode starting up",
        env=settings.app_env,
        version=settings.app_version,
        debug=settings.debug,
    )

    # Connect to databases
    await mongodb.connect()
    await redis_client.connect()

    logger.info("All connections established. ChisCode is ready.")
    yield

    # Cleanup on shutdown
    logger.info("ChisCode shutting down...")
    await mongodb.disconnect()
    await redis_client.disconnect()
    logger.info("Shutdown complete.")


# ── App Factory ───────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="AI-powered agent builder — natural language to production-ready apps",
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=lifespan,
    )

    # ── Middleware ────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_base_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # TrustedHostMiddleware — blocks requests with unexpected Host headers.
    # Needs your HF Spaces hostname in ALLOWED_HOSTS env var:
    #   ALLOWED_HOSTS = your-username-spacename.hf.space,localhost,127.0.0.1
    # Fallback: if ALLOWED_HOSTS only contains defaults, allow all hosts rather
    # than silently returning 400 to every browser request.
    # TrustedHostMiddleware removed — HF Spaces proxy handles host validation.
    # Re-add if self-hosting behind your own reverse proxy.

    # Request timing middleware
    @app.middleware("http")
    async def add_request_timing(request: Request, call_next):
        start = time.perf_counter()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            method=request.method,
            path=request.url.path,
        )
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Process-Time"] = str(duration_ms)
        logger.info(
            "Request handled",
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    # ── Static Files & Templates ──────────────────────────────
    import os
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
    static_path = os.path.join(frontend_path, "static")
    templates_path = os.path.join(frontend_path, "templates")

    if os.path.exists(static_path):
        app.mount("/static", StaticFiles(directory=static_path), name="static")

    templates = Jinja2Templates(directory=templates_path) if os.path.exists(templates_path) else None

    # ── API Routes ────────────────────────────────────────────
    app.include_router(api_router, prefix="/api/v1")

    # ── Health Check ──────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health_check():
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": settings.app_version,
            "env": settings.app_env,
        }

    @app.get("/health/detailed", tags=["system"])
    async def detailed_health():
        """Deep health check — verifies MongoDB and Redis connectivity."""
        checks = {}

        try:
            await mongodb.get_client().admin.command("ping")
            checks["mongodb"] = "ok"
        except Exception as e:
            checks["mongodb"] = f"error: {e}"

        try:
            await redis_client.get_redis().ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {e}"

        all_ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ok" if all_ok else "degraded", "checks": checks},
        )

    # ── Root Route ───────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    """Serve the main frontend page"""
    return templates.TemplateResponse("index.html", {"request": request, "settings": settings})

# Also add a favicon route to avoid 404s
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return HTMLResponse("")
    
    # ── Frontend Routes ───────────────────────────────────────
    if templates:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def index(request: Request):
            return templates.TemplateResponse("index.html", {"request": request, "settings": settings})

        @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
        async def dashboard(request: Request):
            return templates.TemplateResponse("dashboard/index.html", {"request": request})

        @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
        async def login_page(request: Request):
            return templates.TemplateResponse("auth/login.html", {"request": request})

        @app.get("/register", response_class=HTMLResponse, include_in_schema=False)
        async def register_page(request: Request):
            return templates.TemplateResponse("auth/register.html", {"request": request})

    # ── Exception Handlers ────────────────────────────────────
    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found."})
        if templates:
            return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
        return JSONResponse(status_code=404, content={"detail": "Not found."})

    @app.exception_handler(500)
    async def server_error(request: Request, exc):
        logger.error("Unhandled server error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Our team has been notified."},
        )

    return app


app = create_app()
# Add this right after creating the app
print("="*50)
print("REGISTERED ROUTES:")
for route in app.routes:
    print(f"  {route.path} -> {route.name}")
print("="*50)

# Also check if templates exist
import os
print(f"Templates path exists: {os.path.exists('frontend/templates')}")
print(f"Index.html exists: {os.path.exists('frontend/templates/index.html')}")
