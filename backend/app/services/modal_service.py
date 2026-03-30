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
from typing import Dict, Optional, Tuble
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
# ── Modal Sandbox Service ─────────────────────────────────────

class ModalSandboxService:
    """
    Modal-based sandbox service for previewing generated projects.
    Uses gVisor isolation for secure code execution.
    """
    
    def __init__(self):
        self.api_key = settings.modal_api_key
        self.app = None
        
    def _get_app(self):
        """Get or create Modal app reference."""
        import modal
        if self.app is None:
            # Create or lookup your Modal app
            self.app = modal.App.lookup("chiscode-previews", create_if_missing=True)
        return self.app
    
    async def create_sandbox(
        self,
        project_id: str,
        project_name: str,
        file_tree: Dict[str, str],
        stack: Dict,
    ) -> Dict:
        """
        Create a Modal sandbox, upload all files, start the app.
        Returns { sandbox_id, preview_url, port }
        """
        import modal
        
        start_cmd, port = _detect_start_command(file_tree, stack)
        
        logger.info(
            "Creating Modal sandbox",
            project_id=project_id,
            cmd=start_cmd[:200],  # Truncate for logs
            port=port
        )
        
        # Generate file creation commands
        file_setup_commands = self._generate_file_setup_commands(file_tree)
        
        # Combine file setup with start command
        full_start_cmd = f"""
        cd /tmp/preview &&
        {file_setup_commands} &&
        {start_cmd}
        """
        
        # Run in thread pool for synchronous Modal calls
        loop = asyncio.get_event_loop()
        sandbox = await loop.run_in_executor(
            None,
            self._create_sync,
            full_start_cmd,
            port,
            project_id
        )
        
        logger.info(
            "Sandbox ready",
            sandbox_id=sandbox["sandbox_id"],
            url=sandbox["preview_url"]
        )
        
        # Schedule auto-shutdown
        asyncio.create_task(
            self._auto_shutdown(sandbox["sandbox_id"], SANDBOX_ALIVE_SECONDS)
        )
        
        return sandbox
    
    def _generate_file_setup_commands(self, file_tree: Dict[str, str]) -> str:
        """Generate shell commands to create files in sandbox."""
        commands = []
        
        for filepath, content in file_tree.items():
            # Create parent directory
            dir_path = "/".join(filepath.split("/")[:-1])
            if dir_path:
                commands.append(f"mkdir -p {dir_path}")
            
            # Write file content (escape for shell)
            # Use a here-doc for reliable content writing
            escaped_content = content.replace("'", "'\\''")
            commands.append(f"cat > {filepath} << 'EOF'\n{escaped_content}\nEOF")
        
        return " && ".join(commands)
    
    def _create_sync(
        self,
        start_cmd: str,
        port: int,
        project_id: str
    ) -> Dict:
        """Synchronous sandbox creation — runs in thread pool."""
        import modal
        from modal import Sandbox
        
        app = self._get_app()
        
        # Create sandbox with custom configuration
        sandbox = Sandbox.create(
            # Use a lightweight image with Node.js and Python
            "debian-slim",
            # Command to run in the sandbox
            ["/bin/bash", "-c", start_cmd],
            app=app,
            timeout=SANDBOX_TIMEOUT_SECONDS,
            idle_timeout=SANDBOX_IDLE_TIMEOUT_SECONDS,
            # Expose the port for preview
            encrypted_ports=[port],
            # Resource allocation
            memory=2048,  # 2GB RAM
            cpu=2.0,      # 2 CPU cores
            # Custom environment variables
            env_vars={
                "PROJECT_ID": project_id,
                "NODE_ENV": "development",
                "PYTHONUNBUFFERED": "1"
            }
        )
        
        logger.info("Sandbox created", sandbox_id=sandbox.object_id)
        
        # Wait for sandbox to be ready
        sandbox.wait_for_ready(timeout=60)
        
        # Get tunnel URLs
        tunnels = sandbox.tunnels()
        preview_url = tunnels[port].url
        
        return {
            "sandbox_id": sandbox.object_id,
            "preview_url": preview_url,
            "port": port
        }
    
    async def destroy_sandbox(self, sandbox_id: str) -> None:
        """Destroy a sandbox by ID."""
        import modal
        
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            sandbox.terminate()
            sandbox.detach()  # Clean up local connection
            logger.info("Sandbox destroyed", sandbox_id=sandbox_id)
        except Exception as exc:
            logger.warning(
                "Sandbox destroy failed",
                sandbox_id=sandbox_id,
                error=str(exc)
            )
    
    async def _auto_shutdown(self, sandbox_id: str, delay_s: int) -> None:
        """Auto-destroy sandbox after delay."""
        await asyncio.sleep(delay_s)
        await self.destroy_sandbox(sandbox_id)
    
    async def get_sandbox_status(self, sandbox_id: str) -> Dict:
        """Check if sandbox is still running."""
        import modal
        
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            status = sandbox.status()
            return {
                "status": status.value,
                "id": sandbox_id
            }
        except Exception as exc:
            logger.debug("Sandbox status check failed", sandbox_id=sandbox_id, error=str(exc))
            return {"status": "stopped", "id": sandbox_id}
    
    async def get_sandbox_logs(self, sandbox_id: str, lines: int = 50) -> str:
        """Retrieve sandbox logs for debugging."""
        import modal
        
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            # Modal logs are accessible via the client
            # This returns the sandbox's stdout/stderr
            logs = sandbox.logs()
            return "\n".join(logs[-lines:]) if logs else "No logs available"
        except Exception as exc:
            logger.warning("Failed to get sandbox logs", sandbox_id=sandbox_id, error=str(exc))
            return f"Error retrieving logs: {exc}"
    
    async def exec_command(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 30
    ) -> Dict:
        """Execute a command in an existing sandbox."""
        import modal
        
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            # Execute command and capture output
            proc = sandbox.exec(command, timeout=timeout)
            result = await proc.wait()
            
            return {
                "success": result == 0,
                "stdout": await proc.stdout.read(),
                "stderr": await proc.stderr.read(),
                "exit_code": result
            }
        except Exception as exc:
            logger.warning("Command execution failed", sandbox_id=sandbox_id, error=str(exc))
            return {"success": False, "error": str(exc)}
    
    async def upload_files(
        self,
        sandbox_id: str,
        file_tree: Dict[str, str]
    ) -> None:
        """Upload files to an existing sandbox."""
        import modal
        
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            
            for filepath, content in file_tree.items():
                # Create parent directory if needed
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    await self.exec_command(sandbox_id, f"mkdir -p {dir_path}")
                
                # Upload file content
                # Modal doesn't have direct file upload in sandbox API yet
                # Use exec with cat as workaround
                escaped_content = content.replace("'", "'\\''")
                await self.exec_command(
                    sandbox_id,
                    f"cat > {filepath} << 'EOF'\n{escaped_content}\nEOF"
                )
            
            logger.info("Files uploaded", sandbox_id=sandbox_id, count=len(file_tree))
            
        except Exception as exc:
            logger.warning("File upload failed", sandbox_id=sandbox_id, error=str(exc))
    
    async def restart_app(self, sandbox_id: str, start_cmd: str) -> bool:
        """Restart the application in a running sandbox."""
        try:
            # Kill existing processes on the port
            await self.exec_command(sandbox_id, "pkill -f node || true")
            await self.exec_command(sandbox_id, "pkill -f uvicorn || true")
            
            # Start the app again
            result = await self.exec_command(
                sandbox_id,
                f"cd /tmp/preview && {start_cmd}",
                timeout=10
            )
            
            return result.get("success", False)
            
        except Exception as exc:
            logger.warning("App restart failed", sandbox_id=sandbox_id, error=str(exc))
            return False


# ── For backward compatibility ─────────────────────────────────
# Keep the same interface as DaytonaService

class SandboxService(ModalSandboxService):
    """Alias for backward compatibility with existing code."""
    pass