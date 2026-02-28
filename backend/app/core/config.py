"""
ChisCode — Core Configuration
==============================
Loads and validates all environment variables at startup via pydantic-settings.

Environment resolution order (highest → lowest priority):
  1. Real environment variables (injected by HF Spaces Secrets)
  2. .env file (local development only — never committed)
  3. Field defaults defined below

Key additions vs previous version:
  - mongodb_url defaults to Atlas SRV format (mongodb+srv://)
  - mongodb_tls_ca_file — path to certifi bundle, auto-resolved if blank
  - codestral_base_url hardcoded default to https://codestral.mistral.ai/v1
  - port field (7860 for HF Spaces, 8000 for local)
"""

import ssl
from functools import lru_cache
from typing import Literal

import certifi
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────
    app_name:    str = "ChisCode"
    app_version: str = "0.1.0"
    app_env: Literal["development", "staging", "production"] = "production"
    secret_key:  str = Field(..., min_length=32)
    debug:       bool = False

    # Comma-separated in .env → list here
    # HF Spaces: add your-username-spacename.hf.space
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]

    # Public URL of the deployment — drives OAuth redirects, CORS, WS URLs
    # HF Spaces: https://your-username-your-space-name.hf.space
    frontend_base_url: str = "http://localhost:8000"

    # Port — 7860 for HF Spaces (mandatory), 8000 for local dev
    port: int = 7860

    # ── AI / LLM ─────────────────────────────────────────────────
    # Codestral — primary code-generation model
    # Endpoint is fixed to Mistral's Codestral API; override only for proxies.
    codestral_api_key:  str = Field(default="")
    codestral_base_url: str = "https://codestral.mistral.ai/v1"
    codestral_model:    str = "codestral-latest"

    # LangSmith — LLM observability and tracing
    langchain_api_key:      str  = Field(default="")
    langchain_tracing_v2:   bool = True
    langchain_project:      str  = "chiscode"
    langchain_endpoint:     str  = "https://api.smith.langchain.com"

    # ── MongoDB Atlas ────────────────────────────────────────────
    # Use SRV connection string format for Atlas:
    #   mongodb+srv://username:password@cluster.mongodb.net/?retryWrites=true&w=majority
    #
    # TLS is always enabled when connecting to Atlas — the cluster enforces it.
    # We validate certificates using certifi's CA bundle (more reliable than
    # the sparse system cert store in python:3.11-slim containers).
    mongodb_url: str = Field(
        default="mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority",
    )
    mongodb_db: str = "chiscode"

    # Path to the TLS CA bundle.
    # Leave blank (default) to use certifi.where() automatically.
    # Override only if you supply a custom CA (e.g. corporate MITM proxy).
    mongodb_tls_ca_file: str = ""

    # ── Pinecone ─────────────────────────────────────────────────
    pinecone_api_key:     str = Field(default="")
    pinecone_index:       str = "chiscode-embeddings"
    pinecone_environment: str = "us-east-1"

    # ── Redis (Upstash for HF Spaces) ────────────────────────────
    # Upstash provides a TLS Redis URL in the format:
    #   rediss://default:password@endpoint.upstash.io:6379
    # The rediss:// scheme (double-s) enables TLS automatically in redis-py.
    redis_url:      str = Field(default="redis://localhost:6379/0")
    redis_password: str = Field(default="")

    # ── Auth ─────────────────────────────────────────────────────
    github_client_id:     str = Field(default="")
    github_client_secret: str = Field(default="")
    # HF Spaces: update to https://your-space.hf.space/auth/github/callback
    github_redirect_uri:  str = "http://localhost:8000/auth/github/callback"

    jwt_secret_key:                  str = Field(default="", min_length=0)
    jwt_algorithm:                   str = "HS256"
    jwt_access_token_expire_minutes: int = 1440   # 24 hours
    jwt_refresh_token_expire_days:   int = 30

    # ── Payments ─────────────────────────────────────────────────
    revenuecat_api_key:       str = Field(default="")
    revenuecat_webhook_secret: str = Field(default="")

    # ── Search ───────────────────────────────────────────────────
    duckduckgo_max_results: int = 10

    # ── Rate Limits (requests / day per plan) ────────────────────
    rate_limit_free:   int = 5
    rate_limit_basic:  int = 100
    rate_limit_pro:    int = 1000
    rate_limit_yearly: int = 1000

    # ── Validators ───────────────────────────────────────────────

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v

    @field_validator("mongodb_url", mode="before")
    @classmethod
    def validate_mongodb_url(cls, v: str) -> str:
        if not v.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError(
                "MONGODB_URL must start with mongodb:// or mongodb+srv://. "
                "For Atlas use: mongodb+srv://user:pass@cluster.mongodb.net/..."
            )
        return v

    @model_validator(mode="after")
    def resolve_tls_ca_file(self) -> "Settings":
        """Auto-resolve certifi CA bundle path if not explicitly set."""
        if not self.mongodb_tls_ca_file:
            # certifi.where() returns the absolute path to cacert.pem
            # bundled with the certifi package — always present after pip install.
            object.__setattr__(self, "mongodb_tls_ca_file", certifi.where())
        return self

    # ── Computed properties ───────────────────────────────────────

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_atlas(self) -> bool:
        """True when the connection string points at MongoDB Atlas (SRV scheme)."""
        return self.mongodb_url.startswith("mongodb+srv://")

    @property
    def tls_ssl_context(self) -> ssl.SSLContext:
        """
        Pre-built SSLContext for Atlas connections.
        Uses certifi CA bundle — bypasses the container's sparse system store.
        Cached as a property so it's constructed once per Settings instance.
        """
        ctx = ssl.create_default_context(cafile=self.mongodb_tls_ca_file)
        ctx.verify_mode    = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def get_rate_limit(self, plan: str) -> int:
        """Return the daily request limit for a given subscription plan."""
        return {
            "free":   self.rate_limit_free,
            "basic":  self.rate_limit_basic,
            "pro":    self.rate_limit_pro,
            "yearly": self.rate_limit_yearly,
        }.get(plan, self.rate_limit_free)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the cached Settings singleton.
    Called once at startup — subsequent calls return the same instance.
    """
    return Settings()


# Module-level convenience alias — import this throughout the app:
#   from app.core.config import settings
settings = get_settings()