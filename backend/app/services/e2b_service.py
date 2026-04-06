"""
ChisCode — E2B Sandbox Service
================================
Uses custom pre-built E2B templates per framework.
Each template has the correct runtime pre-installed,
so no runtime installation is needed at sandbox start time.
"""
from __future__ import annotations

import asyncio
import time
import urllib.request
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SANDBOX_TIMEOUT_SECONDS = 10 * 60  # 10 minutes


# ── Template selector ─────────────────────────────────────────

def _select_template(file_tree: dict, stack: dict) -> tuple[str, str]:
    """
    Returns (template_id, template_name) for the project.
    Falls back to base template name if ID not configured.
    """
    frontend = (stack.get("frontend") or "").lower()
    backend  = (stack.get("backend")  or "").lower()
    files    = set(file_tree.keys())

    # Next.js
    if ("next" in frontend or
            "next.config.js" in files or
            "next.config.ts" in files or
            "next.config.mjs" in files):
        tid = settings.e2b_template_nextjs
        return (tid or "chiscode-nextjs", "Next.js")

    # SvelteKit
    if ("svelte" in frontend and
            any(f.startswith("src/routes/") for f in files)):
        tid = settings.e2b_template_sveltekit
        return (tid or "chiscode-sveltekit", "SvelteKit")

    # React / Vite
    if ("react" in frontend or "vite" in frontend or
            "vite.config.js" in files or "vite.config.ts" in files or
            "src/App.jsx" in files or "src/App.tsx" in files):
        tid = settings.e2b_template_react
        return (tid or "chiscode-react", "React/Vite")

    # Vue / Nuxt
    if ("vue" in frontend or "nuxt" in frontend or
            "nuxt.config.js" in files or "nuxt.config.ts" in files):
        tid = settings.e2b_template_vue
        return (tid or "chiscode-vue", "Vue/Nuxt")

    # FastAPI / Python
    if ("fastapi" in backend or "python" in backend):
        tid = settings.e2b_template_fastapi
        return (tid or "chiscode-fastapi", "FastAPI")

    # Django
    if "django" in backend or "manage.py" in files:
        tid = settings.e2b_template_django
        return (tid or "chiscode-django", "Django")

    # Express / Node
    if ("express" in backend or "node" in backend or
            "server.js" in files):
        tid = settings.e2b_template_express
        return (tid or "chiscode-express", "Express")

    # Static HTML
    if "index.html" in files or any(f.endswith(".html") for f in files):
        tid = settings.e2b_template_static
        return (tid or "chiscode-static", "Static")

    # Default fallback — use base nodejs template
    return ("nodejs", "Node.js")


# ── Start command detector ────────────────────────────────────

def _detect_start_command(file_tree: dict, stack: dict) -> tuple[str, int]:
    frontend = (stack.get("frontend") or "").lower()
    backend  = (stack.get("backend")  or "").lower()
    files    = set(file_tree.keys())

    has_next_config  = any(f in files for f in (
                           "next.config.js", "next.config.ts", "next.config.mjs"))
    has_vite_config  = "vite.config.js" in files or "vite.config.ts" in files
    has_react_files  = any(f in files for f in (
                           "src/App.jsx", "src/App.tsx",
                           "src/main.jsx", "src/main.tsx"))
    has_sveltekit    = ("svelte.config.js" in files and
                        any(f.startswith("src/routes/") for f in files))
    has_requirements = "requirements.txt" in files
    main_py_files    = [f for f in files
                        if f == "main.py" or f.endswith("/main.py")]

    def fastapi_cmd(path: str) -> tuple[str, int]:
        module = path.replace("/", ".").replace(".py", "")
        req    = "pip install -r requirements.txt && " if has_requirements else ""
        return (f"{req}uvicorn {module}:app --host 0.0.0.0 --port 8000 --reload", 8000)

    # Hybrid: Next.js + FastAPI
    if has_next_config and main_py_files:
        req    = "pip install -r requirements.txt && " if has_requirements else ""
        module = main_py_files[0].replace("/", ".").replace(".py", "")
        return (
            f"{req}uvicorn {module}:app --host 0.0.0.0 --port 8000 "
            f"> /tmp/backend.log 2>&1 & "
            f"npm install && npm run dev -- --port 3000 --hostname 0.0.0.0",
            3000,
        )

    if has_next_config:
        return "npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000

    if "nuxt.config.js" in files or "nuxt.config.ts" in files:
        return "npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000

    if has_sveltekit:
        return "npm install && npm run dev -- --port 5173 --hostname 0.0.0.0", 5173

    if has_vite_config or has_react_files:
        return "npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173

    if "vue.config.js" in files or "src/App.vue" in files:
        return "npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173

    if "svelte.config.js" in files:
        return "npm install && npm run dev -- --host 0.0.0.0 --port 5173", 5173

    if "angular.json" in files:
        return "npm install && ng serve --host 0.0.0.0 --port 4200", 4200

    if main_py_files:
        return fastapi_cmd(main_py_files[0])

    if "manage.py" in files:
        req = "pip install -r requirements.txt && " if has_requirements else ""
        return f"{req}python manage.py runserver 0.0.0.0:8000", 8000

    if "app.py" in files and "Flask" in file_tree.get("app.py", ""):
        req = "pip install -r requirements.txt && " if has_requirements else ""
        return f"{req}python app.py", 5000

    if "server.js" in files:
        return "npm install && node server.js", 3000

    if "package.json" in files:
        return "npm install && npm start", 3000

    if "go.mod" in files and "main.go" in files:
        return "go mod download && go run main.go", 8080

    if "index.html" in files:
        return "python3 -m http.server 8080", 8080

    # Stack fallback
    if "next"    in frontend: return "npm install && npm run dev -- --port 3000 --hostname 0.0.0.0", 3000
    if "react"   in frontend: return "npm install && npm run dev -- --host 0.0.0.0 --port 5173",    5173
    if "svelte"  in frontend: return "npm install && npm run dev -- --host 0.0.0.0",                5173
    if "vue"     in frontend: return "npm install && npm run dev -- --host 0.0.0.0",                3000
    if "fastapi" in backend or "python" in backend:
        if main_py_files:
            return fastapi_cmd(main_py_files[0])
        return "pip install -r requirements.txt && uvicorn src.main:app --host 0.0.0.0 --port 8000", 8000

    logger.warning("No start command detected — using fallback")
    return "python3 -m http.server 8080", 8080


# ── Vite config patcher ───────────────────────────────────────

def _patch_vite_config(file_tree: dict) -> dict:
    patched = dict(file_tree)

    for key in ("vite.config.js", "vite.config.ts"):
        if key not in patched:
            continue
        content = patched[key]
        if "allowedHosts" in content:
            continue
        if "server:" in content:
            content = content.replace(
                "server:",
                "server: {\n      host: '0.0.0.0',\n"
                "      allowedHosts: ['.e2b.app', '.e2b.dev', 'all'],\n    },\n    _dup:",
                1,
            ).replace("_dup:", "server:")
        else:
            content = content.replace(
                "defineConfig({",
                "defineConfig({\n  server: {\n    host: '0.0.0.0',\n"
                "    allowedHosts: ['.e2b.app', '.e2b.dev', 'all'],\n  },",
                1,
            )
        patched[key] = content

    # SvelteKit: inject vite.config.js if missing
    if "svelte.config.js" in patched and "vite.config.js" not in patched:
        patched["vite.config.js"] = (
            "import { defineConfig } from 'vite';\n"
            "import { sveltekit } from '@sveltejs/kit/vite';\n\n"
            "export default defineConfig({\n"
            "  plugins: [sveltekit()],\n"
            "  server: {\n"
            "    host: '0.0.0.0',\n"
            "    allowedHosts: ['.e2b.app', '.e2b.dev', 'all'],\n"
            "  },\n"
            "});\n"
        )

    return patched


# ── E2B Service ───────────────────────────────────────────────

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
        file_tree = _patch_vite_config(file_tree)

        template_id, template_name = _select_template(file_tree, stack)
        start_cmd,   port          = _detect_start_command(file_tree, stack)

        logger.info("Creating E2B sandbox",
                    project_id=project_id,
                    template=template_name,
                    cmd=start_cmd,
                    port=port)

        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._create_sync,
            file_tree, template_id, template_name, start_cmd, port
        )

        logger.info("E2B sandbox ready",
                    sandbox_id=result["sandbox_id"],
                    url=result["preview_url"])

        asyncio.create_task(
            self._auto_kill(result["sandbox_id"], SANDBOX_TIMEOUT_SECONDS)
        )

        return result

    def _create_sync(
        self,
        file_tree:     dict[str, str],
        template_id:   str,
        template_name: str,
        start_cmd:     str,
        port:          int,
    ) -> dict:
        from e2b import Sandbox

        # ── Create sandbox from custom template ───────────────
        sandbox = Sandbox(
            template_id,
            api_key=self.api_key,
            timeout=SANDBOX_TIMEOUT_SECONDS + 60,
        )
        sandbox_id = sandbox.sandbox_id
        logger.info("Sandbox created",
                    sandbox_id=sandbox_id, template=template_name)

        # ── Write all project files ───────────────────────────
        for filepath, content in file_tree.items():
            try:
                dir_path = "/".join(filepath.split("/")[:-1])
                if dir_path:
                    sandbox.commands.run(
                        f"mkdir -p /home/user/{dir_path}",
                        timeout=10,
                    )
                sandbox.files.write(f"/home/user/{filepath}", content)
            except Exception as exc:
                logger.warning("File write failed",
                               path=filepath, error=str(exc))

        logger.info("Files written", count=len(file_tree))

        # ── Start app ─────────────────────────────────────────
        safe_cmd = start_cmd.replace("'", '"').replace("cd /home/user && ", "")
        sandbox.commands.run(
            f'bash -c "cd /home/user && {safe_cmd} > /tmp/app.log 2>&1 &"',
            background=True,
            user="user",
        )
        logger.info("Start command launched", sandbox_id=sandbox_id)

        # ── Get preview URL ───────────────────────────────────
        host        = sandbox.get_host(port)
        preview_url = f"https://{host}"

        # ── Poll until app responds ───────────────────────────
        deadline  = time.time() + 180
        app_ready = False

        while time.time() < deadline:
            time.sleep(6)
            try:
                req = urllib.request.urlopen(preview_url, timeout=5)
                if req.status < 500:
                    app_ready = True
                    logger.info("App responding", url=preview_url)
                    break
            except Exception:
                # Log progress every 30s
                elapsed = int(time.time() - (deadline - 180))
                if elapsed % 30 < 7:
                    try:
                        log_out = sandbox.commands.run(
                            "tail -10 /tmp/app.log 2>/dev/null",
                            timeout=5, user="user",
                        )
                        if log_out.stdout:
                            logger.info("App log",
                                        elapsed=f"{elapsed}s",
                                        log=log_out.stdout[:300])
                    except Exception:
                        pass

        if not app_ready:
            logger.warning("App did not respond in 3min — returning URL anyway",
                           url=preview_url)

        return {
            "sandbox_id":   sandbox_id,
            "workspace_id": sandbox_id,
            "preview_url":  preview_url,
            "port":         port,
        }

    async def _auto_kill(self, sandbox_id: str, delay_s: int) -> None:
        await asyncio.sleep(delay_s)
        await self.destroy_sandbox(sandbox_id)

    async def destroy_sandbox(self, sandbox_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._kill_sync, sandbox_id)
            logger.info("Sandbox killed", sandbox_id=sandbox_id)
        except Exception as exc:
            logger.warning("Sandbox kill failed",
                           sandbox_id=sandbox_id, error=str(exc))

    def _kill_sync(self, sandbox_id: str) -> None:
        from e2b import Sandbox
        sandbox = Sandbox.connect(sandbox_id, api_key=self.api_key)
        sandbox.kill()

    async def get_sandbox_status(self, sandbox_id: str) -> dict:
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
            Sandbox.connect(sandbox_id, api_key=self.api_key)
            return {"status": "running", "id": sandbox_id}
        except Exception:
            return {"status": "stopped"}

    async def _upload_files(
        self, sandbox_id: str, file_tree: dict[str, str]
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._upload_sync, sandbox_id, file_tree
            )
        except Exception as exc:
            logger.warning("File upload failed", error=str(exc))

    def _upload_sync(self, sandbox_id: str, file_tree: dict[str, str]) -> None:
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

    async def _exec_command(self, sandbox_id: str, command: str) -> dict:
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
            f'bash -c "cd /home/user && {command} > /tmp/app.log 2>&1 &"',
            background=True, user="user",
        )
        return {"output": str(result)}