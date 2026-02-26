---
license: apache-2.0
title: 'AI agent web app builder '
sdk: docker
emoji: 📚
colorFrom: indigo
colorTo: blue
pinned: false
short_description: 'Transform natural language to complete web applications '
---
# ⚡ ChisCode

> AI-powered agent builder — transform natural language into production-ready web applications.

[![CI](https://github.com/your-org/chiscode/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/chiscode/actions/workflows/ci.yml)

---

## What is ChisCode?

ChisCode accepts plain-English descriptions of web applications and generates complete, production-ready codebases — including source code, configuration, Docker support, database schemas, and deployment configs.

**Core tech stack:** FastAPI · HTMX · Alpine.js · MongoDB · Pinecone · LangGraph · Codestral

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Python 3.11+
- Git

### 1. Clone & configure

```bash
git clone https://github.com/your-org/chiscode.git
cd chiscode
cp .env.example .env
# Edit .env and fill in your API keys
```

### 2. Start with Docker Compose

```bash
docker compose up --build
```

The API will be available at: **http://localhost:8000**
API docs (dev only): **http://localhost:8000/docs**

### 3. Run locally (development)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

---

## Project Structure

```
chiscode/
├── backend/
│   ├── app/
│   │   ├── api/v1/          # Route handlers
│   │   │   ├── auth.py      # Registration, login, GitHub OAuth
│   │   │   ├── users.py     # Profile, usage, API keys
│   │   │   ├── projects.py  # Generation, iteration, versions, WS
│   │   │   └── webhooks.py  # RevenueCat subscription events
│   │   ├── agents/          # LangGraph agent definitions (Phase 2+)
│   │   ├── core/
│   │   │   ├── config.py    # Pydantic settings
│   │   │   ├── security.py  # JWT, bcrypt, Fernet, API keys
│   │   │   └── logging.py   # Structured logging (structlog)
│   │   ├── db/
│   │   │   ├── mongodb.py   # Motor async client + indexes
│   │   │   └── redis_client.py # Rate limiting, blacklisting, presence
│   │   ├── schemas/         # Pydantic v2 models (user, project)
│   │   ├── services/        # Business logic layer
│   │   └── main.py          # App factory + lifespan
│   ├── tests/
│   └── Dockerfile
├── frontend/
│   ├── templates/           # Jinja2 + HTMX templates
│   │   ├── base.html        # Layout, noise overlay, HTMX config
│   │   ├── index.html       # Landing page
│   │   ├── auth/            # Login, register
│   │   └── dashboard/       # Main app UI
│   └── static/
│       ├── css/main.css     # Full design system (cyberpunk dark)
│       └── js/main.js       # Toast, WebSocket manager, HTMX hooks
├── scripts/
│   └── mongo-init.js        # Database initialization
├── .github/workflows/ci.yml # GitHub Actions CI
├── docker-compose.yml
└── .env.example
```

---

## API Endpoints

### Auth
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/auth/register` | Email/password signup |
| `POST` | `/api/v1/auth/login` | Login, returns JWT |
| `POST` | `/api/v1/auth/logout` | Invalidate token |
| `POST` | `/api/v1/auth/refresh` | Refresh access token |
| `GET`  | `/api/v1/auth/github` | GitHub OAuth redirect |
| `GET`  | `/api/v1/auth/github/callback` | OAuth callback |

### Users
| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/v1/users/me` | Current user profile |
| `GET`  | `/api/v1/users/me/usage` | Daily request usage |
| `POST` | `/api/v1/users/me/api-key` | Generate API key (Pro+) |
| `DELETE` | `/api/v1/users/me/api-key` | Revoke API key |

### Projects
| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/v1/projects/` | List user's projects |
| `POST` | `/api/v1/projects/generate` | Start generation |
| `WS`   | `/projects/ws/{id}` | Live progress stream |
| `POST` | `/api/v1/projects/{id}/confirm` | Approve & commit |
| `POST` | `/api/v1/projects/{id}/cancel` | Cancel project |
| `POST` | `/api/v1/projects/{id}/iterate` | Submit iteration |
| `GET`  | `/api/v1/projects/{id}/versions` | Version history |
| `POST` | `/api/v1/projects/{id}/rollback/{v}` | Rollback to version |

---

## Running Tests

```bash
cd backend
pytest tests/ -v
```

---

## Subscription Plans

| Plan | Requests/Day | API Key | Price |
|------|-------------|---------|-------|
| Free | 5 | ✗ | $0 |
| Basic | 100 | ✗ | $25/mo |
| Pro | 1,000 | ✓ | $120/mo |
| Yearly | 1,000 | ✓ | $1,000/yr |

---

## Environment Variables

See `.env.example` for the complete list. Key variables:

```bash
CODESTRAL_API_KEY    # Mistral Codestral API (core LLM)
LANGCHAIN_API_KEY    # LangSmith observability
MONGODB_URL          # MongoDB connection string
PINECONE_API_KEY     # Vector database for RAG
REDIS_URL            # Session cache & rate limiting
GITHUB_CLIENT_ID     # GitHub OAuth App
GITHUB_CLIENT_SECRET # GitHub OAuth App
REVENUECAT_API_KEY   # Subscription management
SECRET_KEY           # App secret (min 32 chars)
JWT_SECRET_KEY       # JWT signing key (min 32 chars)
```

---

## Build Phases

This foundation covers **Phase 0 and Phase 1** of the 8-phase roadmap:

- ✅ **Phase 0** — Foundation (Docker, FastAPI, MongoDB, Redis, CI)
- ✅ **Phase 1** — Auth (JWT, GitHub OAuth, rate limiting, sessions)
- 🔲 **Phase 2** — Core AI Agent (LangGraph + Codestral)
- 🔲 **Phase 3** — GitHub Integration & Version Control
- 🔲 **Phase 4** — Iteration & Refinement
- 🔲 **Phase 5** — RAG, Code Quality & Templates
- 🔲 **Phase 6** — Preview & Deployment
- 🔲 **Phase 7** — Payments (RevenueCat)
- 🔲 **Phase 8** — Collaborative Features

---

## License

MIT © ChisCode Team