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
from daytona import Daytona, DaytonaConfig


logger = get_logger(__name__)

SANDBOX_ALIVE_SECONDS = 10 * 60  # 10 minutes


# ── Stack → start command + port ──────────────────────────────

def _detect_start_command(file_tree: dict, stack: dict) -> tuple[str, int]:
    """
    Detect project type from actual file structure and return (start_command, port).
    
    Supported project types:
    - Hybrid: Next.js + FastAPI (both servers)
    - Full-stack: Next.js (includes API routes), Nuxt, SvelteKit
    - Frontend only: React (Vite), Vue, Svelte, Angular
    - Backend only: FastAPI, Express, Django, Flask
    - Static: HTML/CSS/JS
    """
    frontend = (stack.get("frontend") or "").lower()
    backend  = (stack.get("backend")  or "").lower()
    files    = set(file_tree.keys())
    
    # ─────────────────────────────────────────────────────────────
    # 1. HYBRID: NEXT.JS + FASTAPI (Python backend + JS frontend)
    # ─────────────────────────────────────────────────────────────
    has_next_config = "next.config.js" in files or "next.config.ts" in files or "next.config.mjs" in files
    has_fastapi_file = "main.py" in files and "FastAPI" in file_tree.get("main.py", "")
    has_requirements = "requirements.txt" in files
    
    if has_next_config and has_fastapi_file:
        logger.info("Detected hybrid: Next.js + FastAPI")
        start_cmd = """
        cd /home/daytona && \
        pip install -r requirements.txt && \
        uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/backend.log 2>&1 & \
        npm install && \
        npm run dev -- --port 3000 --hostname 0.0.0.0
        """
        return start_cmd, 3000
    
    # ─────────────────────────────────────────────────────────────
    # 2. HYBRID: NEXT.JS + EXPRESS (JS backend + JS frontend)
    # ─────────────────────────────────────────────────────────────
    has_express_file = "server.js" in files or "app.js" in files
    has_package_json = "package.json" in files
    
    if has_next_config and has_express_file:
        logger.info("Detected hybrid: Next.js + Express")
        start_cmd = """
        cd /home/daytona && \
        npm install && \
        node server.js > /tmp/backend.log 2>&1 & \
        npm run dev -- --port 3000 --hostname 0.0.0.0
        """
        return start_cmd, 3000
    
    # ─────────────────────────────────────────────────────────────
    # 3. HYBRID: REACT + FASTAPI (Python backend + React frontend)
    # ─────────────────────────────────────────────────────────────
    has_vite_config = "vite.config.js" in files or "vite.config.ts" in files
    has_react_files = "src/App.jsx" in files or "src/App.tsx" in files or "src/main.jsx" in files
    
    if (has_vite_config or has_react_files) and has_fastapi_file:
        logger.info("Detected hybrid: React + Vite + FastAPI")
        start_cmd = """
        cd /home/daytona && \
        pip install -r requirements.txt && \
        uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/backend.log 2>&1 & \
        npm install && \
        npm run dev -- --host 0.0.0.0 --port 5173
        """
        return start_cmd, 5173
    
    # ─────────────────────────────────────────────────────────────
    # 4. FULL-STACK: NEXT.JS ONLY (includes API routes)
    # ─────────────────────────────────────────────────────────────
    if has_next_config:
        logger.info("Detected full-stack: Next.js")
        return "cd /home/daytona && npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    
    # Check package.json for Next.js
    if has_package_json:
        import json
        try:
            pkg = json.loads(file_tree["package.json"])
            dependencies = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in dependencies:
                logger.info("Detected Next.js from package.json")
                return "cd /home/daytona && npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
        except:
            pass
    
    # ─────────────────────────────────────────────────────────────
    # 5. FULL-STACK: NUXT.JS (Vue full-stack)
    # ─────────────────────────────────────────────────────────────
    if "nuxt.config.js" in files or "nuxt.config.ts" in files:
        logger.info("Detected full-stack: Nuxt.js")
        return "cd /home/daytona && npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    
    # ─────────────────────────────────────────────────────────────
    # 6. FULL-STACK: SVELTEKIT
    # ─────────────────────────────────────────────────────────────
    if "svelte.config.js" in files and ("src/routes" in files or "src/app.html" in files):
        logger.info("Detected full-stack: SvelteKit")
        return "cd /home/daytona && npm install && npm run dev -- --port 5173 --hostname 0.0.0.0", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 7. FRONTEND ONLY: REACT (Vite)
    # ─────────────────────────────────────────────────────────────
    if has_vite_config or has_react_files:
        logger.info("Detected frontend: React + Vite")
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 8. FRONTEND ONLY: VUE (Vite)
    # ─────────────────────────────────────────────────────────────
    if "vue.config.js" in files or "src/App.vue" in files:
        logger.info("Detected frontend: Vue")
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 9. FRONTEND ONLY: SVELTE
    # ─────────────────────────────────────────────────────────────
    if "svelte.config.js" in files or "src/App.svelte" in files:
        logger.info("Detected frontend: Svelte")
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 10. FRONTEND ONLY: ANGULAR
    # ─────────────────────────────────────────────────────────────
    if "angular.json" in files:
        logger.info("Detected frontend: Angular")
        return "cd /home/daytona && npm install && ng serve --host 0.0.0.0 --port 4200", 4200
    
    # ─────────────────────────────────────────────────────────────
    # 11. BACKEND ONLY: FASTAPI
    # ─────────────────────────────────────────────────────────────
    if has_fastapi_file:
        logger.info("Detected backend: FastAPI")
        return "cd /home/daytona && pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000", 8000
    
    # Check for FastAPI with different file structure
    if "app/main.py" in files:
        logger.info("Detected backend: FastAPI (app/main.py)")
        return "cd /home/daytona && pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000", 8000
    
    # ─────────────────────────────────────────────────────────────
    # 12. BACKEND ONLY: DJANGO
    # ─────────────────────────────────────────────────────────────
    if "manage.py" in files:
        logger.info("Detected backend: Django")
        return "cd /home/daytona && pip install -r requirements.txt && python manage.py runserver 0.0.0.0:8000", 8000
    
    # ─────────────────────────────────────────────────────────────
    # 13. BACKEND ONLY: FLASK
    # ─────────────────────────────────────────────────────────────
    if "app.py" in files:
        content = file_tree.get("app.py", "")
        if "Flask" in content:
            logger.info("Detected backend: Flask")
            return "cd /home/daytona && pip install -r requirements.txt && python app.py", 5000
    
    # ─────────────────────────────────────────────────────────────
    # 14. BACKEND ONLY: EXPRESS / NODE.JS
    # ─────────────────────────────────────────────────────────────
    if has_express_file:
        logger.info("Detected backend: Express")
        return "cd /home/daytona && npm install && node server.js", 3000
    
    # Check package.json for Express
    if has_package_json:
        import json
        try:
            pkg = json.loads(file_tree["package.json"])
            scripts = pkg.get("scripts", {})
            if "start" in scripts and not has_next_config:
                logger.info("Detected Node.js backend with start script")
                return "cd /home/daytona && npm install && npm start", 3000
        except:
            pass
    
    # ─────────────────────────────────────────────────────────────
    # 15. BACKEND ONLY: GO (Gin)
    # ─────────────────────────────────────────────────────────────
    if "go.mod" in files:
        if "main.go" in files:
            logger.info("Detected backend: Go")
            return "cd /home/daytona && go mod download && go run main.go", 8080
    
    # ─────────────────────────────────────────────────────────────
    # 16. BACKEND ONLY: RUST (Axum)
    # ─────────────────────────────────────────────────────────────
    if "Cargo.toml" in files:
        if "src/main.rs" in files:
            logger.info("Detected backend: Rust")
            return "cd /home/daytona && cargo run --release", 8080
    
    # ─────────────────────────────────────────────────────────────
    # 17. STATIC: HTML/CSS/JS
    # ─────────────────────────────────────────────────────────────
    if "index.html" in files or any(f.endswith(".html") for f in files):
        logger.info("Detected static: HTML/CSS/JS")
        return "cd /home/daytona && python3 -m http.server 8080", 8080
    
    # ─────────────────────────────────────────────────────────────
    # 18. FALLBACK FROM STACK DICTIONARY
    # ─────────────────────────────────────────────────────────────
    if "next" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    if "react" in frontend or "vite" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    if "vue" in frontend or "nuxt" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0", 3000
    if "svelte" in frontend:
        return "cd /home/daytona && npm install && npm run dev -- --host 0.0.0.0", 5173
    if "angular" in frontend:
        return "cd /home/daytona && npm install && ng serve --host 0.0.0.0 --port 4200", 4200
    if "fastapi" in backend or "python" in backend:
        if "main.py" in files:
            return "cd /home/daytona && pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000", 8000
        if "app/main.py" in files:
            return "cd /home/daytona && pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000", 8000
    if "express" in backend or "node" in backend:
        if "server.js" in files:
            return "cd /home/daytona && npm install && node server.js", 3000
        return "cd /home/daytona && npm install && npm start", 3000
    if "django" in backend:
        return "cd /home/daytona && pip install -r requirements.txt && python manage.py runserver 0.0.0.0:8000", 8000
    if "flask" in backend:
        return "cd /home/daytona && pip install -r requirements.txt && python app.py", 5000
    
    # ─────────────────────────────────────────────────────────────
    # 19. ULTIMATE FALLBACK
    # ─────────────────────────────────────────────────────────────
    logger.warning("No start command detected, using fallback")
    return "cd /home/daytona && echo 'No start command detected' && sleep 30", 8080
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

        # Replace the fixed sleep with a poll loop
        import time
        import urllib.request

        # Wait for app to start — poll instead of fixed sleep
        start_wait = time.time()
        app_ready  = False
        while time.time() - start_wait < 120:  # wait up to 2 minutes
            time.sleep(5)
            try:
                req = urllib.request.urlopen(preview_url, timeout=5)
                if req.status < 500:
                    app_ready = True
                    break
            except Exception:
                pass  # still starting

        if not app_ready:
            logger.warning("App did not respond in time", url=preview_url)
            
        # ── Get preview URL ───────────────────────────────────
        try:
            preview_link = sandbox.get_preview_link(port)
            # Handle PortPreviewUrl object
            if hasattr(preview_link, 'url'):
                preview_url = preview_link.url
            else:
                preview_url = str(preview_link)
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
        sandboxes = daytona.list()
        sandbox = next((s for s in sandboxes if s.id == workspace_id), None)
        if not sandbox:
            raise RuntimeError(f"Sandbox {workspace_id} not found or already stopped")
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
        sandboxes = daytona.list()
        sandbox = next((s for s in sandboxes if s.id == workspace_id), None)
        if not sandbox:
            raise RuntimeError(f"Sandbox {workspace_id} not found or already stopped")
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
        sandboxes = daytona.list()
        sandbox = next((s for s in sandboxes if s.id == workspace_id), None)
        if not sandbox:
            raise RuntimeError(f"Sandbox {workspace_id} not found or already stopped")
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
        sandboxes = daytona.list()
        sandbox = next((s for s in sandboxes if s.id == workspace_id), None)
        if not sandbox:
            raise RuntimeError(f"Sandbox {workspace_id} not found or already stopped")
        result  = sandbox.process.exec(
            f"nohup sh -c '{command}' > /tmp/app.log 2>&1 &",
            timeout=10,
        )
        return {"output": str(result)}