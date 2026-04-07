"""
ChisCode — E2B Template Builder
================================
Run once to build all framework templates on E2B.
Reads existing template IDs from environment to skip already-built ones.
Output: prints template IDs to add to HF Spaces secrets.
"""
import os
import subprocess
import tempfile
import json

E2B_API_KEY = os.environ.get("E2B_API_KEY", "")

TEMPLATES = {
    "chiscode-nextjs": {
        "dockerfile": """FROM node:20-slim
WORKDIR /home/user
RUN npm install -g npm@latest
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
    "chiscode-sveltekit": {
        "dockerfile": """FROM node:20-slim
WORKDIR /home/user
RUN npm install -g npm@latest
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
    "chiscode-react": {
        "dockerfile": """FROM node:20-slim
WORKDIR /home/user
RUN npm install -g npm@latest vite
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
    "chiscode-vue": {
        "dockerfile": """FROM node:20-slim
WORKDIR /home/user
RUN npm install -g npm@latest
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
    "chiscode-fastapi": {
        "dockerfile": """FROM python:3.11-slim
WORKDIR /home/user
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx pydantic python-dotenv sqlalchemy alembic
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
    "chiscode-django": {
        "dockerfile": """FROM python:3.11-slim
WORKDIR /home/user
RUN pip install --no-cache-dir django djangorestframework python-dotenv psycopg2-binary
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
    "chiscode-express": {
        "dockerfile": """FROM node:20-slim
WORKDIR /home/user
RUN npm install -g npm@latest nodemon
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
    "chiscode-static": {
        "dockerfile": """FROM python:3.11-slim
WORKDIR /home/user
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*
""",
    },
}


def build_template(name: str, dockerfile_content: str) -> str | None:
    """Build a single E2B template and return its ID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write Dockerfile
        dockerfile_path = os.path.join(tmpdir, "e2b.Dockerfile")
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)

        print(f"\n🔨 Building {name}...")

        try:
            result = subprocess.run(
                ["e2b", "template", "build",
                 "--name", name,
                 "--path", tmpdir],
                capture_output=True,
                text=True,
                timeout=600,  # 10 min max per template
                env={**os.environ, "E2B_API_KEY": E2B_API_KEY},
            )

            output = result.stdout + result.stderr
            print(output[:1000])

            # Parse template ID from output
            # E2B prints something like: "✅ Building sandbox template abc123xyz finished"
            for line in output.split("\n"):
                if "finished" in line.lower() and "Building" in line:
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if len(part) > 8 and part.replace("-", "").isalnum():
                            # Likely the template ID
                            if i > 0 and parts[i-1] != "template":
                                return part

            # Alternative: check for ID pattern directly
            import re
            id_match = re.search(r'\b([a-z0-9]{8,})\b', output)
            if id_match and result.returncode == 0:
                return id_match.group(1)

            if result.returncode != 0:
                print(f"❌ Failed to build {name}: {result.stderr[:300]}")
                return None

        except subprocess.TimeoutExpired:
            print(f"⏱ Timeout building {name}")
            return None
        except FileNotFoundError:
            print("❌ e2b CLI not found — install with: pip install e2b")
            return None


def main():
    if not E2B_API_KEY:
        print("❌ E2B_API_KEY not set")
        return

    print("🚀 Building ChisCode E2B Templates")
    print("=" * 50)

    results = {}

    for name, config in TEMPLATES.items():
        # Check if already built via env var
        env_key = f"E2B_TEMPLATE_{name.replace('chiscode-', '').upper().replace('-', '_')}"
        existing = os.environ.get(env_key, "")
        if existing:
            print(f"✅ {name} already built: {existing} (skipping)")
            results[name] = existing
            continue

        template_id = build_template(name, config["dockerfile"])
        if template_id:
            results[name] = template_id
            print(f"✅ {name}: {template_id}")
        else:
            print(f"❌ {name}: failed")

    print("\n" + "=" * 50)
    print("📋 Add these to your HF Spaces secrets:\n")
    for name, tid in results.items():
        env_key = f"E2B_TEMPLATE_{name.replace('chiscode-', '').upper().replace('-', '_')}"
        print(f"{env_key}={tid}")

    print("\n📄 JSON output:")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()