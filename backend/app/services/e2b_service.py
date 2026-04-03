"""
ChisCode — Daytona Sandbox Service
====================================
Spins up live dev environments from generated file trees.
Uses official Daytona Python SDK.
"""
from __future__ import annotations

import asyncio
import json
import time 
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.logging import get_logger


logger = get_logger(__name__)

SANDBOX_ALIVE_SECONDS = 10 * 60  # 10 minutes
SANDBOX_TIMEOUT_SECONDS = 20 * 60
SANDBOX_IDLE_TIMEOUT_SECONDS = 5 * 60

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
        # E2B starts in /workspace by default - no cd needed
        start_cmd = """
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
        return "npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    
    # Check package.json for Next.js
    if has_package_json:
        import json
        try:
            pkg = json.loads(file_tree["package.json"])
            dependencies = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in dependencies:
                logger.info("Detected Next.js from package.json")
                return "npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
        except:
            pass
    
    # ─────────────────────────────────────────────────────────────
    # 5. FULL-STACK: NUXT.JS (Vue full-stack)
    # ─────────────────────────────────────────────────────────────
    if "nuxt.config.js" in files or "nuxt.config.ts" in files:
        logger.info("Detected full-stack: Nuxt.js")
        return "npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    
    # ─────────────────────────────────────────────────────────────
    # 6. FULL-STACK: SVELTEKIT
    # ─────────────────────────────────────────────────────────────
    if "svelte.config.js" in files and ("src/routes" in files or "src/app.html" in files):
        logger.info("Detected full-stack: SvelteKit")
        return "npm install && npm run dev -- --port 5173 --hostname 0.0.0.0", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 7. FRONTEND ONLY: REACT (Vite)
    # ─────────────────────────────────────────────────────────────
    if has_vite_config or has_react_files:
        logger.info("Detected frontend: React + Vite")
        return "npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 8. FRONTEND ONLY: VUE (Vite)
    # ─────────────────────────────────────────────────────────────
    if "vue.config.js" in files or "src/App.vue" in files:
        logger.info("Detected frontend: Vue")
        return "npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 9. FRONTEND ONLY: SVELTE
    # ─────────────────────────────────────────────────────────────
    if "svelte.config.js" in files or "src/App.svelte" in files:
        logger.info("Detected frontend: Svelte")
        return "npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    
    # ─────────────────────────────────────────────────────────────
    # 10. FRONTEND ONLY: ANGULAR
    # ─────────────────────────────────────────────────────────────
    if "angular.json" in files:
        logger.info("Detected frontend: Angular")
        return "npm install && ng serve --host 0.0.0.0 --port 4200", 4200
    
    # ─────────────────────────────────────────────────────────────
    # 11. BACKEND ONLY: FASTAPI
    # ─────────────────────────────────────────────────────────────
    if has_fastapi_file:
        logger.info("Detected backend: FastAPI")
        return "pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000", 8000
    
    # Check for FastAPI with different file structure
    if "app/main.py" in files:
        logger.info("Detected backend: FastAPI (app/main.py)")
        return "pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000", 8000
    
    # ─────────────────────────────────────────────────────────────
    # 12. BACKEND ONLY: DJANGO
    # ─────────────────────────────────────────────────────────────
    if "manage.py" in files:
        logger.info("Detected backend: Django")
        return "pip install -r requirements.txt && python manage.py runserver 0.0.0.0:8000", 8000
    
    # ─────────────────────────────────────────────────────────────
    # 13. BACKEND ONLY: FLASK
    # ─────────────────────────────────────────────────────────────
    if "app.py" in files:
        content = file_tree.get("app.py", "")
        if "Flask" in content:
            logger.info("Detected backend: Flask")
            return "pip install -r requirements.txt && python app.py", 5000
    
    # ─────────────────────────────────────────────────────────────
    # 14. BACKEND ONLY: EXPRESS / NODE.JS
    # ─────────────────────────────────────────────────────────────
    if has_express_file:
        logger.info("Detected backend: Express")
        return "npm install && node server.js", 3000
    
    # Check package.json for Express
    if has_package_json:
        import json
        try:
            pkg = json.loads(file_tree["package.json"])
            scripts = pkg.get("scripts", {})
            if "start" in scripts and not has_next_config:
                logger.info("Detected Node.js backend with start script")
                return "npm install && npm start", 3000
        except:
            pass
    
    # ─────────────────────────────────────────────────────────────
    # 15. BACKEND ONLY: GO (Gin)
    # ─────────────────────────────────────────────────────────────
    if "go.mod" in files:
        if "main.go" in files:
            logger.info("Detected backend: Go")
            return "go mod download && go run main.go", 8080
    
    # ─────────────────────────────────────────────────────────────
    # 16. BACKEND ONLY: RUST (Axum)
    # ─────────────────────────────────────────────────────────────
    if "Cargo.toml" in files:
        if "src/main.rs" in files:
            logger.info("Detected backend: Rust")
            return "cargo run --release", 8080
    
    # ─────────────────────────────────────────────────────────────
    # 17. STATIC: HTML/CSS/JS
    # ─────────────────────────────────────────────────────────────
    if "index.html" in files or any(f.endswith(".html") for f in files):
        logger.info("Detected static: HTML/CSS/JS")
        return "python3 -m http.server 8080", 8080
    
    # ─────────────────────────────────────────────────────────────
    # 18. FALLBACK FROM STACK DICTIONARY
    # ─────────────────────────────────────────────────────────────
    if "next" in frontend:
        return "npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    if "react" in frontend or "vite" in frontend:
        return "npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173
    if "vue" in frontend or "nuxt" in frontend:
        return "npm install && npm run dev -- --host 0.0.0.0", 3000
    if "svelte" in frontend:
        return "npm install && npm run dev -- --host 0.0.0.0", 5173
    if "angular" in frontend:
        return "npm install && ng serve --host 0.0.0.0 --port 4200", 4200
    if "fastapi" in backend or "python" in backend:
        if "main.py" in files:
            return "pip install -r requirements.txt && uvicorn main:app --host 0.0.0.0 --port 8000", 8000
        if "app/main.py" in files:
            return "pip install -r requirements.txt && uvicorn app.main:app --host 0.0.0.0 --port 8000", 8000
    if "express" in backend or "node" in backend:
        if "server.js" in files:
            return "npm install && node server.js", 3000
        return "npm install && npm start", 3000
    if "django" in backend:
        return "pip install -r requirements.txt && python manage.py runserver 0.0.0.0:8000", 8000
    if "flask" in backend:
        return "pip install -r requirements.txt && python app.py", 5000
    
    # ─────────────────────────────────────────────────────────────
    # 19. ULTIMATE FALLBACK
    # ─────────────────────────────────────────────────────────────
    logger.warning("No start command detected, using fallback")
    return "echo 'No start command detected' && sleep 30", 8080

def _patch_vite_config(file_tree: dict) -> dict:
    """
    Patch vite.config.js/ts to allow E2B proxy hostnames.
    Prevents 'host not allowed' 403 errors in Vite dev server.
    """
    vite_key = None
    for key in ("vite.config.js", "vite.config.ts"):
        if key in file_tree:
            vite_key = key
            break

    # Also handle SvelteKit — vite config is inside svelte.config.js
    # but Vite server options go in a separate vite.config file
    # For SvelteKit, patch svelte.config.js vite server block
    if not vite_key and "svelte.config.js" in file_tree:
        content = file_tree["svelte.config.js"]
        if "allowedHosts" not in content:
            file_tree = dict(file_tree)
            file_tree["vite.config.js"] = """import { defineConfig } from 'vite';
import { sveltekit } from '@sveltejs/kit/vite';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    host: '0.0.0.0',
    allowedHosts: ['.e2b.app', '.e2b.dev', 'all'],
  },
});
"""
        return file_tree

    if not vite_key:
        return file_tree

    content = file_tree[vite_key]
    if "allowedHosts" in content:
        return file_tree  # already patched

    file_tree = dict(file_tree)

    # Try to inject into existing server block
    if "server:" in content or "server: {" in content:
        patched = content.replace(
            "server: {",
            "server: {\n      allowedHosts: ['.e2b.app', '.e2b.dev', 'all'],",
            1,
        )
    elif "defineConfig({" in content:
        # Inject a server block
        patched = content.replace(
            "defineConfig({",
            "defineConfig({\n  server: { host: '0.0.0.0', "
            "allowedHosts: ['.e2b.app', '.e2b.dev', 'all'] },",
            1,
        )
    else:
        patched = content  # can't patch safely, leave as-is

    file_tree[vite_key] = patched
    return file_tree    

    # In your e2b_service.py, before creating sandbox
    logger.info("=== START COMMAND DEBUG ===")
    logger.info("Full start command", cmd=start_cmd)
    logger.info("Working directory", wd="/workspace")
    logger.info("Port", port=port)
# ── E2B Service ────────────────────────────────────────────────

class E2BService:

    def __init__(self):
        self.api_key = settings.e2b_api_key

    async def create_sandbox(
        self,
        project_id:   str,
        project_name: str,
        file_tree:    dict[str, str],
        stack:        dict,
    ) -> dict:
        """
        Create an E2B sandbox, write all files, start the app.
        Returns { sandbox_id, preview_url, port }
        """
        # Patch Vite config so preview URLs work
        file_tree = _patch_vite_config(file_tree)

        start_cmd, port = _detect_start_command(file_tree, stack)

        logger.info("Creating E2B sandbox",
                    project_id=project_id, cmd=start_cmd, port=port)

        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._create_sync, file_tree, start_cmd, port, stack,
        )

        logger.info("E2B sandbox ready",
                    sandbox_id=result["sandbox_id"],
                    url=result["preview_url"])

        # Schedule auto-kill
        asyncio.create_task(
            self._auto_kill(result["sandbox_id"], SANDBOX_TIMEOUT_SECONDS)
        )

        return result

    def _create_sync(
        self,
        file_tree: dict[str, str],
        start_cmd: str,
        port:      int,
        stack:     dict,
    ) -> dict:
        """Synchronous sandbox creation — runs in thread pool."""
        from e2b import Sandbox

        # Create sandbox with timeout
        sandbox    = Sandbox(
            api_key=self.api_key,
            timeout=SANDBOX_TIMEOUT_SECONDS + 60,
        )
        sandbox_id = sandbox.sandbox_id

        logger.info("E2B sandbox created", sandbox_id=sandbox_id)

        # ── Write all project files ───────────────────────────
        for filepath, content in file_tree.items():
            try:
                # Ensure parent directory exists
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    sandbox.commands.run(
                        f"mkdir -p /home/user/{dir_path}",
                        timeout=10,
                    )
                sandbox.files.write(
                    f"/home/user/{filepath}",
                    content,
                )
            except Exception as exc:
                logger.warning("File write failed",
                               path=filepath, error=str(exc))

        logger.info("Files written to E2B sandbox",
                    count=len(file_tree))

        # ── Start the app using a background session ──────────────────
        if needs_node:
            logger.info("Installing Node.js via nvm", sandbox_id=sandbox_id)
        # Replace the start command run with:
        nvm_prefix = (
            "export NVM_DIR=\"$HOME/.nvm\" && "
            "[ -s \"$NVM_DIR/nvm.sh\" ] && . \"$NVM_DIR/nvm.sh\" && "
        )
        full_cmd = nvm_prefix + start_cmd.replace("cd /home/user && ", "cd /home/user && " + nvm_prefix)

        sandbox.commands.run(
            f"bash -c 'cd /home/user && {nvm_prefix}"
            f"{start_cmd.replace(\"cd /home/user && \", \"\")} "
            f"> /tmp/app.log 2>&1 &'",
            timeout=10,
            user="user",
        )

        # ── Install Python deps if needed ─────────────────────────────
        needs_python = any(x in backend for x in ("fastapi", "python", "django"))
        if needs_python:
            sandbox.commands.run(
                "pip install uvicorn fastapi httpx",
                timeout=60,
            )

        # ── Start the app ─────────────────────────────────────────────
        sandbox.commands.run(
            "bash",
            "-c",
            f"cd /home/user && {start_cmd.replace('cd /home/user && ', '')} > /tmp/app.log 2>&1",
            background=True,
            timeout=5,
        )

        # ── Get public preview URL ────────────────────────────────────
        host        = sandbox.get_host(port)
        preview_url = f"https://{host}"

        # ── Poll up to 90 seconds for app to respond ─────────────────
        import urllib.request
        deadline = time.time() + 90
        app_ready = False
        while time.time() < deadline:
            time.sleep(4)
            try:
                req = urllib.request.urlopen(preview_url, timeout=5)
                if req.status < 500:
                    app_ready = True
                    break
            except Exception:
                continue

        if not app_ready:
            logger.warning("App did not respond in 90s — returning URL anyway",
                           url=preview_url)
            
        return {
            "sandbox_id":  sandbox_id,
            "workspace_id": sandbox_id,
            "preview_url": preview_url,
            "port":        port,
        }

    async def _auto_kill(self, sandbox_id: str, delay_s: int) -> None:
        """Kill sandbox after delay."""
        await asyncio.sleep(delay_s)
        await self.destroy_sandbox(sandbox_id)

    async def destroy_sandbox(self, sandbox_id: str) -> None:
        """Kill an E2B sandbox by ID."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._kill_sync, sandbox_id
            )
            logger.info("E2B sandbox killed", sandbox_id=sandbox_id)
        except Exception as exc:
            logger.warning("E2B sandbox kill failed",
                           sandbox_id=sandbox_id, error=str(exc))

    def _kill_sync(self, sandbox_id: str) -> None:
        from e2b import Sandbox
        sandbox = Sandbox.connect(sandbox_id, api_key=self.api_key)
        sandbox.kill()

    async def get_sandbox_status(self, sandbox_id: str) -> dict:
        """Check if sandbox is still running."""
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._status_sync, sandbox_id
            )
        except Exception:
            return {"status": "stopped"}

    def _status_sync(self, sandbox_id: str) -> dict:
        from e2b import Sandbox
        try:
            sandbox = Sandbox.connect(sandbox_id, api_key=self.api_key)
            # If connect succeeds the sandbox is running
            return {"status": "running", "id": sandbox_id}
        except Exception:
            return {"status": "stopped"}

    async def _upload_files(
        self,
        sandbox_id: str,
        file_tree:  dict[str, str],
    ) -> None:
        """Upload files to existing sandbox (for self-heal redeploy)."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._upload_sync, sandbox_id, file_tree
            )
        except Exception as exc:
            logger.warning("E2B file upload failed", error=str(exc))

    def _upload_sync(
        self,
        sandbox_id: str,
        file_tree:  dict[str, str],
    ) -> None:
        from e2b import Sandbox
        sandbox = Sandbox.connect(sandbox_id, api_key=self.api_key)
        for filepath, content in file_tree.items():
            try:
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    sandbox.commands.run(
                        f"mkdir -p /home/user/{dir_path}", timeout=10
                    )
                sandbox.files.write(f"/home/user/{filepath}", content)
            except Exception as exc:
                logger.warning("File write failed",
                               path=filepath, error=str(exc))

    async def _exec_command(
        self,
        sandbox_id: str,
        command:    str,
    ) -> dict:
        """Execute command in existing sandbox."""
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._exec_sync, sandbox_id, command
            )
        except Exception as exc:
            return {"error": str(exc)}

    def _exec_sync(self, sandbox_id: str, command: str) -> dict:
        from e2b import Sandbox
        sandbox = Sandbox.connect(sandbox_id, api_key=self.api_key)
        result  = sandbox.commands.run(
            f"nohup sh -c '{command}' > /tmp/app.log 2>&1 &",
            timeout=10,
        )
        return {"output": result.stdout}