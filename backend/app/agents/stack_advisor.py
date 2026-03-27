"""
ChisCode — Smart Stack Advisor
================================
LangGraph node that analyzes the user's prompt and suggests
the best tech stack with rationale. Returns multiple options
ranked by fit — the user picks one (HITL) before generation starts.

Stack categories covered:
  Frontend:  HTML/CSS/JS, React, Next.js, Vue, Svelte
  Backend:   FastAPI, Express/Node.js, Next.js API Routes
  Database:  SQLite, PostgreSQL, MongoDB, Supabase
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mistralai import ChatMistralAI
import json, re

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Updated Stack Knowledge with Better Architecture Rules ──

STACK_KNOWLEDGE = """
ARCHITECTURE PRINCIPLES:
- Next.js IS a full-stack framework (includes API routes) — never combine with separate backend
- For full-stack web apps, Next.js alone is the optimal choice
- Only add separate backend (FastAPI/Express) when:
  * User specifically requests Python backend
  * Project needs heavy background processing
  * Real-time features require WebSockets
  * Existing team prefers Python/Node.js for backend

APP TYPE → RECOMMENDED STACKS

full_stack_web_app / saas / dashboard:
  - Next.js + Prisma + PostgreSQL + Tailwind (single codebase, Vercel-ready)
  - Next.js + Supabase (PostgreSQL + Auth + Realtime built-in)

frontend_only / static_site:
  - React + Vite + Tailwind (SPA, deploy to Vercel/Netlify)
  - HTML + CSS + Vanilla JS (zero dependencies)

backend_api_only:
  - FastAPI + Python (AI/ML, rapid prototyping)
  - Express + Node.js (JS ecosystem)
  - FastAPI + PostgreSQL (production-ready API)

frontend + separate_backend (React/Vue + FastAPI):
  - React + Vite + FastAPI + PostgreSQL
  - Use when: Python backend needed OR team prefers separation

real_time_app / chat:
  - Next.js + Supabase Realtime (simplest)
  - React + FastAPI + WebSockets + Redis (if Python backend required)

data_app / analytics:
  - Streamlit + Python (fastest data apps)
  - React + FastAPI + Pandas (production-grade)

ai_app / chatbot:
  - Next.js + Vercel AI SDK (simplest, streaming ready)
  - React + FastAPI + LangChain (if Python AI libs needed)

COMPLEXITY RULES:
  simple   → Single runtime (HTML only, or Next.js only)
  moderate → One framework (Next.js) or separated but simple
  complex  → Microservices, multiple databases, background workers
"""


# ── Updated Main Function with Better Guardrails ──

async def suggest_stacks(
    prompt:      str,
    app_type:    str,
    complexity:  str,
    features:    list[str],
) -> list[dict[str, Any]]:
    """
    Returns a list of 3 stack options with intelligent architecture choices.
    Ensures no redundant combinations like Next.js + FastAPI.
    """
    llm = ChatMistralAI(
        model=settings.codestral_model,
        api_key=settings.codestral_api_key,
        base_url=settings.codestral_base_url,
        temperature=0.2,
        max_tokens=2048,
    )

    system = SystemMessage(content=f"""You are a senior software architect advising on tech stack selection.

CRITICAL RULES:
1. NEVER suggest Next.js + a separate backend (FastAPI/Express). Next.js HAS built-in API routes.
2. For full-stack apps, recommend Next.js alone as the default.
3. Only recommend React + FastAPI if:
   - User explicitly wants Python backend, OR
   - Project needs Python libraries (AI/ML), OR
   - User wants clear frontend/backend separation
4. For simple apps, prefer single-runtime solutions (HTML only, or Next.js only).

{STACK_KNOWLEDGE}

Return ONLY a valid JSON array of exactly 3 stack options. No markdown, no explanation.

Each option:
{{
  "id": "option_a" | "option_b" | "option_c",
  "label": "Short name e.g. Next.js + PostgreSQL",
  "frontend": "e.g. Next.js 15 + Tailwind",
  "backend": "e.g. Next.js API Routes" or "FastAPI (Python)" or "None",
  "database": "e.g. PostgreSQL" or "SQLite" or "None",
  "extras": ["lib1", "lib2"],
  "rationale": "2 sentences explaining why this architecture fits",
  "complexity_fit": "simple" | "moderate" | "complex",
  "pros": ["pro1", "pro2", "pro3"],
  "cons": ["con1", "con2"]
}}

Order: best fit first. Provide variety but respect the rules above.
""")

    human = HumanMessage(content=f"""App description: {prompt}
App type: {app_type}
Complexity: {complexity}
Key features: {', '.join(features) if features else 'none specified'}

Suggest 3 tech stacks following the architecture rules.""")

    try:
        response = await llm.ainvoke([system, human])
        raw = response.content.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        options = json.loads(raw)
        if not isinstance(options, list):
            raise ValueError("Expected list")
        
        # Post-process to ensure no invalid combinations
        options = _validate_and_fix_options(options, app_type, complexity)
        return options[:3]
        
    except Exception as exc:
        logger.warning("Stack advisor failed, using defaults", error=str(exc))
        return _default_stacks(app_type, complexity)


def _validate_and_fix_options(options: list[dict], app_type: str, complexity: str) -> list[dict]:
    """
    Validate options and fix any invalid combinations (like Next.js + separate backend)
    """
    fixed_options = []
    
    for opt in options:
        label = opt.get("label", "").lower()
        frontend = opt.get("frontend", "").lower()
        backend = opt.get("backend", "").lower()
        
        # Fix: Next.js should never have separate backend
        if ("next" in label or "next" in frontend) and "fastapi" in backend or "express" in backend:
            logger.warning("Fixing invalid combination: Next.js + separate backend")
            opt["backend"] = "Next.js API Routes"
            opt["label"] = opt["label"].replace(" + FastAPI", "").replace(" + Express", "")
            opt["rationale"] = "Next.js includes built-in API routes, eliminating need for a separate backend."
        
        # If React frontend has no backend, keep as is
        # If React frontend has FastAPI, keep as is (valid combination)
        
        fixed_options.append(opt)
    
    return fixed_options


def _default_stacks(app_type: str, complexity: str) -> list[dict]:
    """
    Intelligent fallback stacks based on app type and complexity
    """
    # Default stacks based on app type
    if app_type == "dashboard" or app_type == "full_stack_web_app":
        return [
            {
                "id": "option_a",
                "label": "Next.js + PostgreSQL",
                "frontend": "Next.js 15 + Tailwind CSS",
                "backend": "Next.js API Routes",
                "database": "PostgreSQL + Prisma ORM",
                "extras": ["shadcn/ui", "NextAuth.js", "Tailwind CSS"],
                "rationale": "Next.js provides everything you need: frontend, API routes, and server components. One codebase, one deployment.",
                "complexity_fit": "moderate",
                "pros": ["Single codebase", "Built-in API routes", "Vercel-ready", "SSR/SSG support"],
                "cons": ["JavaScript/TypeScript only", "Learning curve for App Router"],
            },
            {
                "id": "option_b",
                "label": "Next.js + Supabase",
                "frontend": "Next.js 15 + Tailwind",
                "backend": "Next.js API Routes",
                "database": "Supabase (PostgreSQL + Auth + Realtime)",
                "extras": ["shadcn/ui", "Supabase Client"],
                "rationale": "Supabase adds authentication, realtime, and storage without managing a separate backend.",
                "complexity_fit": "moderate",
                "pros": ["Built-in auth", "Realtime subscriptions", "File storage", "Auto-generated APIs"],
                "cons": ["Vendor lock-in", "Monthly cost at scale"],
            },
            {
                "id": "option_c",
                "label": "React + FastAPI + PostgreSQL",
                "frontend": "React + Vite + Tailwind",
                "backend": "FastAPI (Python 3.11)",
                "database": "PostgreSQL + SQLAlchemy",
                "extras": ["shadcn/ui", "Alembic migrations"],
                "rationale": "Clean separation between frontend and backend. Best if you prefer Python or need AI/ML libraries.",
                "complexity_fit": "complex",
                "pros": ["Type-safe APIs with Pydantic", "Python ecosystem", "Clear separation of concerns"],
                "cons": ["Two runtimes to manage", "CORS configuration needed", "More infrastructure"],
            },
        ]
    
    elif app_type == "api_only":
        return [
            {
                "id": "option_a",
                "label": "FastAPI + PostgreSQL",
                "frontend": "None",
                "backend": "FastAPI (Python 3.11)",
                "database": "PostgreSQL + SQLAlchemy",
                "extras": ["Pydantic", "Alembic", "Uvicorn"],
                "rationale": "FastAPI is the fastest Python API framework with automatic OpenAPI docs.",
                "complexity_fit": "simple",
                "pros": ["Automatic API docs", "Async support", "Type validation"],
                "cons": ["Python runtime overhead", "Not as fast as Go/Rust"],
            },
            {
                "id": "option_b",
                "label": "Express + MongoDB",
                "frontend": "None",
                "backend": "Express.js + Node.js",
                "database": "MongoDB + Mongoose",
                "extras": ["JWT auth", "Express middleware"],
                "rationale": "JavaScript ecosystem, great for rapid development with flexible schema.",
                "complexity_fit": "simple",
                "pros": ["Massive npm ecosystem", "Flexible schema", "Great for MVPs"],
                "cons": ["No built-in validation", "Manual OpenAPI setup"],
            },
            {
                "id": "option_c",
                "label": "FastAPI + SQLite",
                "frontend": "None",
                "backend": "FastAPI (Python)",
                "database": "SQLite + SQLAlchemy",
                "extras": ["Pydantic", "Uvicorn"],
                "rationale": "Zero-config database, perfect for local development and small projects.",
                "complexity_fit": "simple",
                "pros": ["No separate database server", "Easy to deploy", "Lightweight"],
                "cons": ["Not suitable for high concurrency", "Limited features"],
            },
        ]
    
    # Generic default stacks that avoid redundancy
    return [
        {
            "id": "option_a",
            "label": "Next.js + SQLite",
            "frontend": "Next.js + Tailwind CSS",
            "backend": "Next.js API Routes",
            "database": "SQLite + Prisma ORM",
            "extras": [],
            "rationale": "Full-stack Next.js with a single file database. Simplest production-ready option.",
            "complexity_fit": "simple",
            "pros": ["Single codebase", "No separate backend", "Easy deployment"],
            "cons": ["SQLite not for high scale", "JavaScript only"],
        },
        {
            "id": "option_b",
            "label": "React + FastAPI + PostgreSQL",
            "frontend": "React + Vite + Tailwind",
            "backend": "FastAPI (Python)",
            "database": "PostgreSQL",
            "extras": ["shadcn/ui", "SQLAlchemy"],
            "rationale": "Clean separation with Python backend. Ideal for AI features or Python-heavy logic.",
            "complexity_fit": "moderate",
            "pros": ["Python ecosystem", "Type-safe APIs", "Scalable database"],
            "cons": ["Two services to deploy", "CORS setup needed"],
        },
        {
            "id": "option_c",
            "label": "HTML + CSS + Vanilla JS",
            "frontend": "HTML5 + CSS3 + JavaScript",
            "backend": "None",
            "database": "None",
            "extras": [],
            "rationale": "Zero dependencies, works everywhere. Perfect for simple static sites.",
            "complexity_fit": "simple",
            "pros": ["No build step", "Fastest load times", "Universal compatibility"],
            "cons": ["No component system", "Manual state management"],
        },
    ]