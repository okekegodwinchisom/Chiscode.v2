"""
ChisCode — Daytona Sandbox Service
====================================
Spins up live dev environments from generated file trees.
Uses Daytona Cloud API to create, manage, and destroy sandboxes.
"""
from __future__ import annotations

import asyncio
import tempfile
import os
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

DAYTONA_API_BASE = "https://app.daytona.io/api"
SANDBOX_ALIVE_MINUTES = 10


# ── Stack → start command mapping ─────────────────────────────

def _detect_start_command(file_tree: dict, stack: dict) -> tuple[str, int]:
    """
    Returns (start_command, port) based on stack and files present.
    """
    frontend = (stack.get("frontend") or "").lower()
    backend  = (stack.get("backend")  or "").lower()
    files    = set(file_tree.keys())

    # Next.js
    if "next" in frontend:
        return "npm install && npm run dev", 3000

    # React / Vite
    if "react" in frontend or "vite" in frontend:
        return "npm install && npm run dev", 5173

    # Vue / Nuxt
    if "vue" in frontend or "nuxt" in frontend:
        return "npm install && npm run dev", 3000

    # SvelteKit
    if "svelte" in frontend:
        return "npm install && npm run dev", 5173

    # FastAPI
    if "fastapi" in backend or "python" in backend:
        if "main.py" in files:
            return "pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000", 8000
        if "app/main.py" in files:
            return "pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000", 8000

    # Express / Node
    if "express" in backend or "node" in backend:
        if "server.js" in files:
            return "npm install && node server.js", 3000
        return "npm install && npm start", 3000

    # Vanilla HTML — serve with Python
    if "index.html" in files:
        return "python3 -m http.server 8080", 8080

    # Default fallback
    return "npm install && npm start", 3000


def _detect_language(stack: dict, file_tree: dict) -> str:
    """Detect primary language for Daytona workspace."""
    backend  = (stack.get("backend")  or "").lower()
    frontend = (stack.get("frontend") or "").lower()

    if "python" in backend or "fastapi" in backend or "django" in backend:
        return "python"
    if "next" in frontend or "react" in frontend or "vue" in frontend:
        return "javascript"
    if any(f.endswith(".ts") or f.endswith(".tsx") for f in file_tree):
        return "typescript"
    return "javascript"


# ── Daytona API client ─────────────────────────────────────────

class DaytonaService:

    def __init__(self):
        self.api_key  = settings.daytona_api_key
        self.base_url = DAYTONA_API_BASE
        self.headers  = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

    async def create_sandbox(
        self,
        project_id:   str,
        project_name: str,
        file_tree:    dict[str, str],
        stack:        dict,
    ) -> dict:
        """
        Create a Daytona workspace, upload files, start the app.
        Returns { workspace_id, preview_url, port }
        """
        start_cmd, port = _detect_start_command(file_tree, stack)
        language        = _detect_language(stack, file_tree)

        logger.info("Creating Daytona sandbox",
                    project_id=project_id, cmd=start_cmd, port=port)

        # ── Step 1: Create workspace ──────────────────────────
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/workspace",
                headers=self.headers,
                json={
                    "name":     f"chiscode-{project_id[:8]}",
                    "language": language,
                    "env":      {},
                },
            )
            resp.raise_for_status()
            workspace = resp.json()
            workspace_id = workspace["id"]

        logger.info("Daytona workspace created", workspace_id=workspace_id)

        # ── Step 2: Upload files ──────────────────────────────
        await self._upload_files(workspace_id, file_tree)

        # ── Step 3: Run start command ─────────────────────────
        await self._exec_command(workspace_id, start_cmd)

        # ── Step 4: Wait for app to be ready ──────────────────
        preview_url = await self._wait_for_ready(workspace_id, port)

        # ── Step 5: Schedule auto-shutdown ────────────────────
        asyncio.create_task(
            self._auto_shutdown(workspace_id, SANDBOX_ALIVE_MINUTES * 60)
        )

        return {
            "workspace_id": workspace_id,
            "preview_url":  preview_url,
            "port":         port,
        }

    async def _upload_files(
        self,
        workspace_id: str,
        file_tree:    dict[str, str],
    ) -> None:
        """Upload all project files to the workspace."""
        async with httpx.AsyncClient(timeout=120) as client:
            for filepath, content in file_tree.items():
                resp = await client.post(
                    f"{self.base_url}/workspace/{workspace_id}/file",
                    headers=self.headers,
                    json={
                        "path":    filepath,
                        "content": content,
                    },
                )
                if resp.status_code not in (200, 201):
                    logger.warning("File upload failed",
                                   path=filepath, status=resp.status_code)

        logger.info("Files uploaded", workspace_id=workspace_id,
                    count=len(file_tree))

    async def _exec_command(
        self,
        workspace_id: str,
        command:      str,
    ) -> dict:
        """Execute a command in the workspace."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/workspace/{workspace_id}/exec",
                headers=self.headers,
                json={"command": command},
            )
            resp.raise_for_status()
            return resp.json()

    async def _wait_for_ready(
        self,
        workspace_id: str,
        port:         int,
        max_wait_s:   int = 60,
    ) -> str:
        """
        Poll until the app is responding on the expected port.
        Returns the public preview URL.
        """
        # Get the preview URL from Daytona
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}/workspace/{workspace_id}/preview/{port}",
                headers=self.headers,
            )
            resp.raise_for_status()
            data        = resp.json()
            preview_url = data.get("url", "")

        if not preview_url:
            raise RuntimeError("Daytona did not return a preview URL")

        # Poll until app responds
        start = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(timeout=10) as client:
            while (asyncio.get_event_loop().time() - start) < max_wait_s:
                try:
                    r = await client.get(preview_url, follow_redirects=True)
                    if r.status_code < 500:
                        logger.info("App ready", url=preview_url)
                        return preview_url
                except Exception:
                    pass
                await asyncio.sleep(3)

        logger.warning("App did not respond in time", url=preview_url)
        return preview_url  # Return anyway — Playwright will handle timeout

    async def _auto_shutdown(
        self,
        workspace_id: str,
        delay_s:      int,
    ) -> None:
        """Auto-destroy workspace after delay."""
        await asyncio.sleep(delay_s)
        await self.destroy_sandbox(workspace_id)

    async def destroy_sandbox(self, workspace_id: str) -> None:
        """Destroy a Daytona workspace."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(
                    f"{self.base_url}/workspace/{workspace_id}",
                    headers=self.headers,
                )
                resp.raise_for_status()
            logger.info("Daytona sandbox destroyed", workspace_id=workspace_id)
        except Exception as exc:
            logger.warning("Failed to destroy sandbox",
                           workspace_id=workspace_id, error=str(exc))

    async def get_sandbox_status(self, workspace_id: str) -> dict:
        """Get current status of a workspace."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}/workspace/{workspace_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()