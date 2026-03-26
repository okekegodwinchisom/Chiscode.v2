"""
ChisCode — Daytona Sandbox Service
====================================
Spins up live dev environments from generated file trees.
Uses official Daytona Python SDK.
"""
from __future__ import annotations

import asyncio
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SANDBOX_ALIVE_SECONDS = 10 * 60  # 10 minutes


# ── Stack → start command + port ──────────────────────────────

def _detect_start_command(file_tree: dict, stack: dict) -> tuple[str, int]:
    frontend = (stack.get("frontend") or "").lower()
    backend  = (stack.get("backend")  or "").lower()
    files    = set(file_tree.keys())

    if "next" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    if "react" in frontend or "vite" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    if "vue" in frontend or "nuxt" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0", 3000
    if "svelte" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0", 5173
    if "fastapi" in backend or "python" in backend:
        if "main.py" in files:
            return "cd /home/daytona && pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000", 8000
        if "app/main.py" in files:
            return "cd /home/daytona && pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000", 8000
    if "express" in backend or "node" in backend:
        if "server.js" in files:
            return "cd /home/daytona && npm install && node server.js", 3000
        return "cd /home/daytona && npm install && npm start", 3000
    if "index.html" in files:
        return "cd /home/daytona && python3 -m http.server 8080", 8080

    return "cd /home/daytona && npm install && npm start", 3000


# ── Daytona Service ────────────────────────────────────────────

class DaytonaService:

    def __init__(self):
        self.api_key = settings.daytona_api_key

    def _client(self):
        from daytona import Daytona, DaytonaConfig
        return Daytona(DaytonaConfig(
            api_key=self.api_key,
            server_url="https://app.daytona.io/api",
        ))

    async def create_sandbox(
        self,
        project_id:   str,
        project_name: str,
        file_tree:    dict[str, str],
        stack:        dict,
    ) -> dict:
        """
        Create a Daytona sandbox, upload all files, start the app.
        Returns { workspace_id, preview_url, port }
        """
        from daytona import CreateSandboxParams

        start_cmd, port = _detect_start_command(file_tree, stack)

        logger.info("Creating Daytona sandbox",
                    project_id=project_id, cmd=start_cmd, port=port)

        # Run blocking SDK calls in thread pool to avoid blocking event loop
        loop    = asyncio.get_event_loop()
        sandbox = await loop.run_in_executor(None, self._create_sync,
                                             file_tree, start_cmd, port)

        logger.info("Sandbox ready", sandbox_id=sandbox["workspace_id"],
                    url=sandbox["preview_url"])

        # Schedule auto-shutdown
        asyncio.create_task(
            self._auto_shutdown(sandbox["workspace_id"], SANDBOX_ALIVE_SECONDS)
        )

        return sandbox

    def _create_sync(
        self,
        file_tree: dict[str, str],
        start_cmd: str,
        port:      int,
    ) -> dict:
        """Synchronous sandbox creation — runs in thread pool."""
        from daytona import Daytona, DaytonaConfig

        daytona = Daytona(DaytonaConfig(
            api_key=self.api_key,
            server_url="https://app.daytona.io/api",
        ))

        # ── Create sandbox ────────────────────────────────────
            sandbox = daytona.create()
            auto_stop_interval=15,    # stop after 15 min inactivity
        ))

        logger.info("Sandbox created", sandbox_id=sandbox.id)

        # ── Upload files ──────────────────────────────────────
        for filepath, content in file_tree.items():
            try:
                # Create parent directories
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    sandbox.process.exec(
                        f"mkdir -p /home/daytona/{dir_path}",
                        timeout=10,
                    )
                # Upload file content
                sandbox.fs.upload_file(
                    content.encode("utf-8"),
                    f"/home/daytona/{filepath}",
                )
            except Exception as exc:
                logger.warning("File upload failed",
                               path=filepath, error=str(exc))

        logger.info("Files uploaded", count=len(file_tree))

        # ── Start the app in background ───────────────────────
        sandbox.process.exec(
            f"nohup sh -c '{start_cmd}' > /tmp/app.log 2>&1 &",
            timeout=10,
        )

        # Wait for app to start
        import time
        time.sleep(8)

        # ── Get preview URL ───────────────────────────────────
        try:
            preview_url = sandbox.get_preview_link(port)
        except Exception:
            preview_url = f"https://proxy.app.daytona.io/{sandbox.id}/{port}"

        return {
            "workspace_id": sandbox.id,
            "preview_url":  preview_url,
            "port":         port,
        }

    async def _auto_shutdown(self, workspace_id: str, delay_s: int) -> None:
        """Auto-destroy sandbox after delay."""
        await asyncio.sleep(delay_s)
        await self.destroy_sandbox(workspace_id)

    async def destroy_sandbox(self, workspace_id: str) -> None:
        """Destroy a sandbox by ID."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._destroy_sync, workspace_id
            )
            logger.info("Sandbox destroyed", workspace_id=workspace_id)
        except Exception as exc:
            logger.warning("Sandbox destroy failed",
                           workspace_id=workspace_id, error=str(exc))

    def _destroy_sync(self, workspace_id: str) -> None:
        from daytona import Daytona, DaytonaConfig
        daytona = Daytona(DaytonaConfig(
            api_key=self.api_key,
            server_url="https://app.daytona.io/api",
        ))
        sandbox = daytona.get_current_sandbox(workspace_id)
        daytona.delete(sandbox)

    async def get_sandbox_status(self, workspace_id: str) -> dict:
        """Check if sandbox is still running."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._status_sync, workspace_id
            )
        except Exception:
            return {"status": "stopped"}

    def _status_sync(self, workspace_id: str) -> dict:
        from daytona import Daytona, DaytonaConfig
        daytona = Daytona(DaytonaConfig(
            api_key=self.api_key,
            server_url="https://app.daytona.io/api",
        ))
        sandbox = daytona.get_current_sandbox(workspace_id)
        return {"status": "running", "id": sandbox.id}

    async def _upload_files(
        self,
        workspace_id: str,
        file_tree:    dict[str, str],
    ) -> None:
        """Upload files to existing sandbox (for self-heal redeploy)."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, self._upload_sync, workspace_id, file_tree
            )
        except Exception as exc:
            logger.warning("File upload failed", error=str(exc))

    def _upload_sync(
        self,
        workspace_id: str,
        file_tree:    dict[str, str],
    ) -> None:
        from daytona import Daytona, DaytonaConfig
        daytona = Daytona(DaytonaConfig(
            api_key=self.api_key,
            server_url="https://app.daytona.io/api",
        ))
        sandbox = daytona.get_current_sandbox(workspace_id)
        for filepath, content in file_tree.items():
            try:
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    sandbox.process.exec(
                        f"mkdir -p /home/daytona/{dir_path}", timeout=10
                    )
                sandbox.fs.upload_file(
                    content.encode("utf-8"),
                    f"/home/daytona/{filepath}",
                )
            except Exception as exc:
                logger.warning("File upload failed",
                               path=filepath, error=str(exc))

    async def _exec_command(
        self,
        workspace_id: str,
        command:      str,
    ) -> dict:
        """Execute command in existing sandbox."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._exec_sync, workspace_id, command
            )
        except Exception as exc:
            return {"error": str(exc)}

    def _exec_sync(self, workspace_id: str, command: str) -> dict:
        from daytona import Daytona, DaytonaConfig
        daytona = Daytona(DaytonaConfig(
            api_key=self.api_key,
            server_url="https://app.daytona.io/api",
        ))
        sandbox = daytona.get_current_sandbox(workspace_id)
        result  = sandbox.process.exec(
            f"nohup sh -c '{command}' > /tmp/app.log 2>&1 &",
            timeout=10,
        )
        return {"output": str(result)}