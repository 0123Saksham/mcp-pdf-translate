"""Background translation jobs for Claude Desktop's 4-minute MCP tool timeout."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import RUNS_DIR, new_job_dir
from pipeline import run_pipeline_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_job_status(job_dir: Path, **fields) -> dict:
    job_dir.mkdir(parents=True, exist_ok=True)
    status_path = job_dir / "status.json"
    data: dict = {}
    if status_path.is_file():
        data = json.loads(status_path.read_text(encoding="utf-8"))
    data.update(fields)
    data["updated_at"] = _utc_now()
    status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def start_path_job(input_path: str, target_lang: str, output_path: str | None = None) -> dict:
    """Start pipeline in a daemon thread; return immediately with job_id."""
    inp = Path(input_path).resolve()
    if not inp.is_file():
        return {"ok": False, "stage": "input", "error": f"file not found: {inp}"}

    if output_path:
        out = str(Path(output_path).resolve())
    else:
        out = str(inp.parent / f"{inp.stem}_{target_lang}.pdf")

    job_dir = new_job_dir()
    job_id = job_dir.name
    write_job_status(
        job_dir,
        ok=True,
        job_id=job_id,
        status="running",
        stage="queued",
        input_path=str(inp),
        output_path=out,
        target_lang=target_lang,
        started_at=_utc_now(),
    )

    def _worker() -> None:
        try:
            result = run_pipeline_path(
                str(inp),
                target_lang,
                out,
                job_dir=job_dir,
                status_cb=lambda **kw: write_job_status(job_dir, **kw),
            )
            write_job_status(
                job_dir,
                status="done" if result.get("ok") else "failed",
                stage=result.get("stage", "done"),
                result=result,
            )
        except Exception as e:
            write_job_status(
                job_dir,
                status="failed",
                stage="pipeline",
                result={"ok": False, "stage": "pipeline", "error": str(e), "job_dir": str(job_dir)},
            )

    threading.Thread(target=_worker, name=f"pdf-job-{job_id}", daemon=True).start()
    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "job_dir": str(job_dir),
        "input_path": str(inp),
        "output_path": out,
        "message": "Job started. Poll get_translate_pdf_job every 15–30s until status is done or failed.",
    }


def get_path_job(job_id: str) -> dict:
    job_id = job_id.strip()
    if not job_id:
        return {"ok": False, "error": "job_id is required"}

    job_dir = RUNS_DIR / job_id
    status_path = job_dir / "status.json"
    if not status_path.is_file():
        return {"ok": False, "error": f"job not found: {job_id}"}

    data = json.loads(status_path.read_text(encoding="utf-8"))
    data["ok"] = True
    return data
