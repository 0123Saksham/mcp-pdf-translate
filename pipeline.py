"""Full pdf-translate pipeline: extract → translate → check → apply."""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import fitz

from config import (
    APPLY_PY,
    CHECK_PY,
    EXTRACT_PY,
    PYTHON,
    new_job_dir,
)
from translate_lib import run_translate


class PipelineError(Exception):
    def __init__(self, stage: str, message: str, exit_code: int | None = None):
        self.stage = stage
        self.exit_code = exit_code
        super().__init__(message)


_DEBUG_LOG = Path(__file__).resolve().parent / "runs" / "pipeline_debug.log"


def _log(msg: str) -> None:
    import sys
    line = f"[pipeline] {time.strftime('%H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    with _DEBUG_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run_script(stage: str, script: Path, args: list[str], job_dir: Path) -> str:
    cmd = [PYTHON, str(script), *args]
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / f"{stage}.log"
    _log(f"{stage}: launching subprocess → {script.name}")
    t0 = time.monotonic()
    with log_path.open("wb") as logf:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT)
    elapsed = round(time.monotonic() - t0, 2)
    _log(f"{stage}: subprocess done in {elapsed}s (exit={proc.returncode})")
    out = log_path.read_text(encoding="utf-8", errors="replace").strip() if log_path.is_file() else ""
    if proc.returncode != 0:
        raise PipelineError(stage, out or f"{stage} exited {proc.returncode}", proc.returncode)
    return out


def _page_count(pdf_path: Path) -> int:
    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def run_pipeline(
    input_pdf: Path,
    target_lang: str,
    output_pdf: Path,
    *,
    model: str | None = None,
    job_dir: Path | None = None,
) -> dict:
    """
    Run extract → translate → check → apply on disk paths.
    Returns stats dict.
    """
    timings: dict[str, float] = {}
    t_total = time.monotonic()

    if job_dir is None:
        job_dir = new_job_dir()

    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    input_pdf = input_pdf.resolve()
    output_pdf = output_pdf.resolve()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    _log(f"run_pipeline: starting for {input_pdf.name} → {target_lang}")

    t0 = time.monotonic()
    _run_script("extract", EXTRACT_PY, [str(input_pdf), str(work_dir)], job_dir)
    timings["extract"] = round(time.monotonic() - t0, 2)
    _log(f"run_pipeline: extract done in {timings['extract']}s")

    t0 = time.monotonic()
    _log("run_pipeline: starting translate (asyncio.run)...")
    tr_stats = asyncio.run(run_translate(work_dir, target_lang, model))
    timings["translate"] = tr_stats["elapsed_s"]
    _log(f"run_pipeline: translate done in {timings['translate']}s — {tr_stats['strings_translated']} strings")

    t0 = time.monotonic()
    _run_script("check", CHECK_PY, [str(work_dir)], job_dir)
    timings["check"] = round(time.monotonic() - t0, 2)
    _log(f"run_pipeline: check done in {timings['check']}s")

    t0 = time.monotonic()
    _run_script("apply", APPLY_PY, [str(work_dir), str(output_pdf), "--target", target_lang], job_dir)
    timings["apply"] = round(time.monotonic() - t0, 2)
    _log(f"run_pipeline: apply done in {timings['apply']}s")

    if not output_pdf.is_file():
        raise PipelineError("apply", f"expected output not found: {output_pdf}")

    timings["total"] = round(time.monotonic() - t_total, 2)
    _log(f"run_pipeline: total pipeline {timings['total']}s")

    from datetime import datetime, timezone

    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pages": _page_count(output_pdf),
        "strings_translated": tr_stats["strings_translated"],
        "shards": tr_stats["shards"],
        "input_tokens": tr_stats["input_tokens"],
        "output_tokens": tr_stats["output_tokens"],
        "timing_s": timings,
        "input_bytes": input_pdf.stat().st_size,
        "output_bytes": output_pdf.stat().st_size,
        "output_path": str(output_pdf),
        "work_dir": str(work_dir),
        "job_dir": str(job_dir),
    }


def run_pipeline_url(pdf_url: str, target_lang: str, model: str | None = None) -> dict:
    """Download PDF from a URL, run the full pipeline, return stats."""
    import requests as _req

    _log(f"run_pipeline_url: starting, url={pdf_url[:80]}...")
    job_dir = new_job_dir()
    _log(f"run_pipeline_url: job_dir={job_dir}")
    input_pdf = job_dir / "input.pdf"
    output_pdf = job_dir / "output.pdf"

    t_dl = time.monotonic()
    _log("run_pipeline_url: downloading PDF...")
    try:
        resp = _req.get(pdf_url, timeout=120)
        resp.raise_for_status()
    except Exception as e:
        _log(f"run_pipeline_url: download FAILED: {e}")
        return {"ok": False, "stage": "download", "error": f"download failed: {e}", "job_dir": str(job_dir)}

    input_pdf.write_bytes(resp.content)
    download_s = round(time.monotonic() - t_dl, 2)
    _log(f"run_pipeline_url: downloaded {len(resp.content):,} bytes in {download_s}s")

    if input_pdf.stat().st_size < 100:
        return {"ok": False, "stage": "download", "error": "downloaded file is too small — URL may be a page, not a direct download link", "job_dir": str(job_dir)}

    try:
        stats = run_pipeline(input_pdf, target_lang, output_pdf, model=model, job_dir=job_dir)
        stats["timing_s"]["download"] = download_s
        return {
            "ok": True,
            "output_path": str(output_pdf),
            "download_link": None,
            "stats": stats,
        }
    except PipelineError as e:
        return {
            "ok": False,
            "stage": e.stage,
            "error": str(e),
            "exit_code": e.exit_code,
            "job_dir": str(job_dir),
        }
    except Exception as e:
        return {"ok": False, "stage": "pipeline", "error": str(e), "job_dir": str(job_dir)}
