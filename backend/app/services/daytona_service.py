"""
ChisCode — Daytona Sandbox Service
====================================
Spins up live dev environments from generated file trees.
Uses official Daytona SDK for Cloud API.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SANDBOX_ALIVE_SECONDS = 10 * 60  # 10 minutes


# ── Stack → start command mapping ─────────────────────────────

def _detect_start_command(file_tree: dict, stack: dict) -> tuple[str, int]:
    frontend = (stack.get("frontend") or "").lower()
    backend  = (stack.get("backend")  or "").lower()
    files    = set(file_tree.keys())

    if "next" in frontend:
        return "npm install && npm run dev", 3000
    if "react" in frontend or "vite" in frontend:
        return "npm install && npm run dev", 5173
    if "vue" in frontend or "nuxt" in frontend:
        return "npm install && npm run dev", 3000
    if "svelte" in frontend:
        return "npm install && npm run dev", 5173
    if "fastapi" in backend or "python" in backend:
        if "main.py" in files:
            return "pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000", 8000
        if "app/main.py" in files:
            return "pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000", 8000
    if "express" in backend or "node" in backend:
        if "server.js" in files:
            return "npm install && node server.js", 3000
        return "npm install && npm start", 3000
    if "index.html" in files:
        return "python3 -m http.server 8080", 8080

    return "npm install && npm start", 3000


# ── Daytona Service ────────────────────────────────────────────

class DaytonaService:

    def __init__(self):
        self.api_key = settings.daytona_api_key

    async def create_sandbox(
        self,
        project_id:   str,
        project_name: str,
        file_tree:    dict[str, str],
        stack:        dict,
    ) -> dict:
        """
        Create a Daytona sandbox, upload files, start the app.
        Returns { workspace_id, preview_url, port }
        """
        from daytona_sdk import Daytona, DaytonaConfig, CreateSandboxParams

        start_cmd, port = _detect_start_command(file_tree, stack)

        logger.info("Creating Daytona sandbox",
                    project_id=project_id,
                    cmd=start_cmd,
                    port=port)

        # ── Init Daytona client ───────────────────────────────
        config  = DaytonaConfig(api_key=self.api_key)
        daytona = Daytona(config)

        # ── Create sandbox ────────────────────────────────────
        sandbox = daytona.create(CreateSandboxParams(
            language="python",  # container language — not the project language
        ))

        logger.info("Sandbox created", sandbox_id=sandbox.id)

        # ── Write files ───────────────────────────────────────
        for filepath, content in file_tree.items():
            try:
                # Ensure parent directory exists
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    sandbox.process.exec(f"mkdir -p /home/user/{dir_path}")

                # Write file content
                # Escape content for shell
                escaped = content.replace("'", "'\\''")
                sandbox.process.exec(
                    f"cat > /home/user/{filepath} << 'CHISCODE_EOF'\n"
                    f"{content}\n"
                    f"CHISCODE_EOF"
                )
            except Exception as exc:
                logger.warning("File write failed",
                               path=filepath, error=str(exc))

        logger.info("Files written to sandbox",
                    count=len(file_tree))

        # ── Run start command ─────────────────────────────────
        sandbox.process.exec(
            f"cd /home/user && nohup sh -c '{start_cmd}' "
            f"> /tmp/app.log 2>&1 &"
        )

        # ── Get preview URL ───────────────────────────────────
        await asyncio.sleep(5)  # wait for app to start

        try:
            preview_url = sandbox.get_preview_link(port)
        except Exception:
            preview_url = f"https://{sandbox.id}-{port}.daytona.app"

        logger.info("Sandbox ready", url=preview_url)

        # ── Schedule auto-shutdown ────────────────────────────
        asyncio.create_task(
            self._auto_shutdown(daytona, sandbox, SANDBOX_ALIVE_SECONDS)
        )

        return {
            "workspace_id": sandbox.id,
            "preview_url":  preview_url,
            "port":         port,
        }

    async def _auto_shutdown(self, daytona, sandbox, delay_s: int) -> None:
        """Auto-destroy sandbox after delay."""
        await asyncio.sleep(delay_s)
        try:
            daytona.remove(sandbox)
            logger.info("Sandbox auto-destroyed", sandbox_id=sandbox.id)
        except Exception as exc:
            logger.warning("Sandbox destroy failed",
                           sandbox_id=sandbox.id, error=str(exc))

    async def destroy_sandbox(self, workspace_id: str) -> None:
        """Manually destroy a sandbox by ID."""
        try:
            from daytona_sdk import Daytona, DaytonaConfig
            config  = DaytonaConfig(api_key=self.api_key)
            daytona = Daytona(config)
            # Get sandbox by ID and remove
            sandbox = daytona.get_current_sandbox(workspace_id)
            daytona.remove(sandbox)
            logger.info("Sandbox destroyed", workspace_id=workspace_id)
        except Exception as exc:
            logger.warning("Failed to destroy sandbox",
                           workspace_id=workspace_id, error=str(exc))

    async def get_sandbox_status(self, workspace_id: str) -> dict:
        """Get current status of a sandbox."""
        try:
            from daytona_sdk import Daytona, DaytonaConfig
            config  = DaytonaConfig(api_key=self.api_key)
            daytona = Daytona(config)
            sandbox = daytona.get_current_sandbox(workspace_id)
            return {"status": "running", "id": sandbox.id}
        except Exception:
            return {"status": "stopped"}

    async def _upload_files(
        self,
        workspace_id: str,
        file_tree:    dict[str, str],
    ) -> None:
        """Upload files to existing sandbox (for redeploy after self-heal)."""
        try:
            from daytona_sdk import Daytona, DaytonaConfig
            config  = DaytonaConfig(api_key=self.api_key)
            daytona = Daytona(config)
            sandbox = daytona.get_current_sandbox(workspace_id)

            for filepath, content in file_tree.items():
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    sandbox.process.exec(f"mkdir -p /home/user/{dir_path}")
                sandbox.process.exec(
                    f"cat > /home/user/{filepath} << 'CHISCODE_EOF'\n"
                    f"{content}\n"
                    f"CHISCODE_EOF"
                )
        except Exception as exc:
            logger.warning("File upload to sandbox failed", error=str(exc))

    async def _exec_command(
        self,
        workspace_id: str,
        command:      str,
    ) -> dict:
        """Execute command in existing sandbox."""
        try:
            from daytona_sdk import Daytona, DaytonaConfig
            config  = DaytonaConfig(api_key=self.api_key)
            daytona = Daytona(config)
            sandbox = daytona.get_current_sandbox(workspace_id)
            result  = sandbox.process.exec(
                f"cd /home/user && nohup sh -c '{command}' "
                f"> /tmp/app.log 2>&1 &"
            )
            return {"output": str(result)}
        except Exception as exc:
            logger.warning("Exec failed", error=str(exc))
            return {"error": str(exc)}