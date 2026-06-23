#!/usr/bin/env python3
"""Smoke-test pipeline stages without MCP. Skips translate if --extract-only."""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402 — loads .env from skill root

from pipeline import run_pipeline_base64, run_pipeline_path  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Test pdf-translate MCP pipeline locally")
    p.add_argument("pdf", nargs="?", help="Path to input PDF")
    p.add_argument("--target", "-t", default="en", help="Target language code")
    p.add_argument("--output", "-o", help="Output PDF path")
    p.add_argument("--base64-roundtrip", action="store_true", help="Test base64 in/out")
    p.add_argument("--extract-only", action="store_true", help="Run extract+check only (no API)")
    args = p.parse_args()

    default_pdf = Path(os.environ.get("USERPROFILE", "")) / "Downloads" / "50_pages_french.pdf"
    pdf = Path(args.pdf) if args.pdf else default_pdf
    if not pdf.is_file():
        print(f"PDF not found: {pdf}", file=sys.stderr)
        return 1

    if args.extract_only:
        from config import EXTRACT_PY, PYTHON
        import subprocess

        job = _ROOT / "runs" / "test_extract"
        job.mkdir(parents=True, exist_ok=True)
        work = job / "work"
        work.mkdir(exist_ok=True)
        subprocess.run([PYTHON, str(EXTRACT_PY), str(pdf), str(work)], check=True)
        shards = work / "shards.json"
        print(f"extract OK → {work}")
        if shards.exists():
            print(f"  parallel mode: {shards}")
        return 0

    if args.base64_roundtrip:
        b64 = base64.b64encode(pdf.read_bytes()).decode("ascii")
        print(f"input base64 size: {len(b64):,} chars")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "ANTHROPIC_API_KEY not set. Add to D:\\Internship\\skill\\.env or run:\n"
                "  $env:ANTHROPIC_API_KEY = 'sk-ant-...'",
                file=sys.stderr,
            )
            return 1
        result = run_pipeline_base64(b64, args.target)
        print(json.dumps({k: v for k, v in result.items() if k != "pdf_base64"}, indent=2))
        if result.get("ok") and result.get("pdf_base64"):
            out = _ROOT / "runs" / "test_roundtrip_out.pdf"
            out.write_bytes(base64.b64decode(result["pdf_base64"]))
            print(f"wrote {out}")
        return 0 if result.get("ok") else 1

    out = Path(args.output) if args.output else _ROOT / "runs" / f"test_{args.target}.pdf"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY not set. Add to D:\\Internship\\skill\\.env or run:\n"
            "  $env:ANTHROPIC_API_KEY = 'sk-ant-...'",
            file=sys.stderr,
        )
        return 1

    result = run_pipeline_path(str(pdf), args.target, str(out))
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
