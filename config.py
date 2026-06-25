"""Paths and defaults for pdf-translate MCP."""
import os
import sys
from pathlib import Path

MCP_ROOT = Path(__file__).resolve().parent

EXTRACT_PY = MCP_ROOT / "extract.py"
CHECK_PY = MCP_ROOT / "check.py"
APPLY_PY = MCP_ROOT / "apply.py"

RUNS_DIR = MCP_ROOT / "runs"
RUNS_DIR.mkdir(exist_ok=True)

PYTHON = sys.executable
DEFAULT_MODEL = os.environ.get("PDF_TRANSLATE_MODEL", "claude-sonnet-4-6")

# Dedicated container for pdf-translate uploads (kept separate from any
# existing app container). Override with AZURE_STORAGE_CONTAINER_NAME.
AZURE_CONTAINER_DEFAULT = "pdf-translate"


def _load_dotenv() -> None:
    """Load ANTHROPIC_API_KEY from .env in mcp root or parent dir."""
    for env_path in (MCP_ROOT / ".env", MCP_ROOT.parent / ".env"):
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Use claude_desktop_config.json env, "
            f"or add it to {MCP_ROOT / '.env'}, or: $env:ANTHROPIC_API_KEY='sk-ant-...'"
        )
    return key


def google_project() -> str:
    """Google Cloud project id for the Translation API.

    Reads GOOGLE_CLOUD_PROJECT, else falls back to the project_id inside the
    service-account JSON pointed to by GOOGLE_APPLICATION_CREDENTIALS.
    """
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if proj:
        return proj
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if key_path and Path(key_path).is_file():
        import json
        try:
            proj = json.loads(Path(key_path).read_text(encoding="utf-8")).get("project_id", "").strip()
        except Exception:
            proj = ""
    if not proj:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is not set and no project_id found in the "
            f"service-account key. Add it to {MCP_ROOT / '.env'} or the MCP env block."
        )
    return proj


def google_location() -> str:
    """Region for translate_text. 'global' is the fastest/most available."""
    return os.environ.get("GOOGLE_TRANSLATE_LOCATION", "global").strip() or "global"


def azure_connection_string() -> str:
    val = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not val:
        raise RuntimeError(
            "AZURE_STORAGE_CONNECTION_STRING is not set. Add it to "
            f"{MCP_ROOT / '.env'} or the MCP server's env block."
        )
    return val


def azure_container_name() -> str:
    return os.environ.get("AZURE_STORAGE_CONTAINER_NAME", "").strip() or AZURE_CONTAINER_DEFAULT


def new_job_dir() -> Path:
    import uuid

    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir
