"""
ChisCode — FastAPI Application
Main app factory with lifespan management, middleware, and routing.
"""
import time
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.router import api_router
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db import mongodb, redis_client

setup_logging()
logger = get_logger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ChisCode starting up", env=settings.app_env, version=settings.app_version)
    try:
        await mongodb.connect()
    except Exception as e:
        logger.error("MongoDB connection failed — running degraded", error=str(e))
    try:
        await redis_client.connect()
    except Exception as e:
        logger.error("Redis connection failed — running degraded", error=str(e))
    logger.info("All connections established. ChisCode is ready.")
    yield
    logger.info("ChisCode shutting down...")
    await mongodb.disconnect()
    await redis_client.disconnect()
    logger.info("Shutdown complete.")

# startup
from app.db import pinecone_client
await pinecone_client.connect()   # graceful — logs warning if key missing

# shutdown
await pinecone_client.disconnect()

db = mongodb.get_db()

# Phase 5: text search on templates
await db["templates"].create_index(
    [("name","text"),("description","text"),("tags","text")],
    name="templates_text_search", background=True,
)
await db["templates"].create_index("app_type",  background=True)
await db["templates"].create_index("is_active", background=True)
await db["templates"].create_index("use_count", background=True)

# Phase 6: TTL on previews (auto-expire at expires_at)
await db["previews"].create_index(
    "expires_at", expireAfterSeconds=0,
    name="previews_ttl", background=True,
)


# ── App Factory ───────────────────────────────────────────────────

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

    # ── Middleware ────────────────────────────────────────────────
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_base_url, "https://huggingface.co"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_request_timing(request: Request, call_next):
        start = time.perf_counter()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(method=request.method, path=request.url.path)
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Process-Time"] = str(duration_ms)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        logger.info("Request handled", status_code=response.status_code, duration_ms=duration_ms)
        return response

    # ── Static files & templates ──────────────────────────────────
    frontend_path  = Path("/app/frontend")
    static_path    = frontend_path / "static"
    templates_path = frontend_path / "templates"

    logger.info("Frontend check",
                path=str(frontend_path),
                exists=frontend_path.exists(),
                static=static_path.exists(),
                templates=templates_path.exists())

    if static_path.exists():
        app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
        logger.info("Static files mounted", path=str(static_path))

    templates = None
    if templates_path.exists():
        templates = Jinja2Templates(directory=str(templates_path))
        logger.info("Templates loaded", path=str(templates_path))
    else:
        logger.warning("Templates not found — HTML routes disabled", path=str(templates_path))
    # After creating templates, make settings globally available
    if templates:
        templates.env.globals["settings"] = settings
    # ── API routes ────────────────────────────────────────────────
    app.include_router(api_router, prefix="/api/v1")

    # ── Health checks ─────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health_check():
        return {"status": "ok", "app": settings.app_name, "version": settings.app_version}

    @app.get("/health/detailed", tags=["system"])
    async def detailed_health():
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

    # ── Frontend HTML routes ──────────────────────────────────────
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

        @app.get("/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False)
        async def project_detail_page(request: Request, project_id: str):
            return templates.TemplateResponse("projects/detail.html", {"request": request})

    # ── Exception handlers ────────────────────────────────────────
    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found."})
        return JSONResponse(status_code=404, content={"detail": "Not found."})
        
    @app.exception_handler(500)
    async def server_error(request: Request, exc):
        logger.error("Unhandled server error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error."})

    return app


app = create_app()
