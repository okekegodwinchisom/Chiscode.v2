# app/services/fragments_templates.py

"""
Fragments-inspired templates for preview generation.
Each template defines how to run a specific type of project.
"""

from typing import Dict, List, Optional

class PreviewTemplate:
    """Base template for preview configurations."""
    
    def __init__(
        self,
        name: str,
        language: str,
        start_command: str,
        port: int,
        dependencies_install: Optional[str] = None,
        file_requirements: List[str] = None
    ):
        self.name = name
        self.language = language
        self.start_command = start_command
        self.port = port
        self.dependencies_install = dependencies_install
        self.file_requirements = file_requirements or []
    
    def matches(self, file_tree: Dict[str, str]) -> bool:
        """Check if this template matches the project."""
        files = set(file_tree.keys())
        return all(req in files for req in self.file_requirements)


# Define all available templates (inspired by Fragments)
PREVIEW_TEMPLATES = [
    PreviewTemplate(
        name="nextjs",
        language="typescript",
        start_command="npm run dev -- --port 3000 --hostname 0.0.0.0",
        port=3000,
        dependencies_install="npm install",
        file_requirements=["next.config.js", "package.json"]
    ),
    PreviewTemplate(
        name="fastapi",
        language="python",
        start_command="uvicorn main:app --host 0.0.0.0 --port 8000",
        port=8000,
        dependencies_install="pip install -r requirements.txt",
        file_requirements=["main.py", "requirements.txt"]
    ),
    PreviewTemplate(
        name="react_vite",
        language="typescript",
        start_command="npm run dev -- --host 0.0.0.0 --port 5173",
        port=5173,
        dependencies_install="npm install",
        file_requirements=["vite.config.js", "package.json"]
    ),
    PreviewTemplate(
        name="static_html",
        language="html",
        start_command="python3 -m http.server 8080",
        port=8080,
        dependencies_install=None,
        file_requirements=["index.html"]
    ),
    PreviewTemplate(
        name="streamlit",
        language="python",
        start_command="streamlit run app.py --server.port 8501 --server.address 0.0.0.0",
        port=8501,
        dependencies_install="pip install -r requirements.txt",
        file_requirements=["app.py"]
    ),
    PreviewTemplate(
        name="gradio",
        language="python",
        start_command="gradio app.py",
        port=7860,
        dependencies_install="pip install -r requirements.txt",
        file_requirements=["app.py"]
    ),
]


def detect_template(file_tree: Dict[str, str], stack: Dict) -> PreviewTemplate:
    """Detect the appropriate template for a project."""
    for template in PREVIEW_TEMPLATES:
        if template.matches(file_tree):
            logger.info("Template detected", template=template.name)
            return template
    
    # Fallback to base template
    logger.warning("No template matched, using base")
    return PreviewTemplate(
        name="base",
        language="unknown",
        start_command="echo 'No start command' && sleep 10",
        port=8080
    )


def generate_fragments_code(file_tree: Dict[str, str], template: PreviewTemplate) -> Dict:
    """
    Generate Fragments-compatible code structure for preview.
    This mirrors how Fragments organizes sandbox code.
    """
    return {
        "template": template.name,
        "language": template.language,
        "port": template.port,
        "files": file_tree,
        "start_command": template.start_command,
        "install_command": template.dependencies_install
    }