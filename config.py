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


def new_job_dir() -> Path:
    import uuid

    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir
