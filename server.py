#!/usr/bin/env python3
"""
pdf-translate MCP server (stdio).

Tool:
  translate_pdf_url — accepts a temporary download URL, server downloads the PDF,
                      runs the full pipeline (extract → translate → check → apply),
                      uploads the result, and returns a download link.
"""
from __future__ import annotations

import json
import os
import sys
import asyncio
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
os.chdir(_ROOT)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP

from pipeline import run_pipeline_url

mcp = FastMCP(
    "pdf-translate",
    instructions=(
        "Translates PDF text layers while preserving layout. "
        "Call translate_pdf_url with a temporary download URL (e.g. tmpfiles.org/dl/...) "
        "and a target language code. The server downloads the PDF, runs the full "
        "pipeline (extract → parallel API translate → check → apply), uploads the "
        "translated PDF, and returns its download link as 'download_link'."
    ),
)


def _json_result(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def translate_pdf_url(pdf_url: str, target_lang: str) -> str:
    """
    Translate a PDF by downloading it from a temporary URL.

    The skill script uploads the user's PDF to a temp file host (e.g. tmpfiles.org)
    and passes the direct download URL here. The server downloads the PDF itself —
    the LLM never touches the file bytes. After translating, the server uploads the
    finished PDF and returns its download link as "download_link".

    Args:
        pdf_url: Direct download URL to a PDF file (must return raw PDF bytes,
                 not an HTML page). Use tmpfiles.org/dl/... format.
        target_lang: ISO language code for output (e.g. en, hi, ar, de, fr).

    Returns JSON with: ok, download_link (URL of translated PDF), output_path, stats.
    """
    if not pdf_url or not pdf_url.strip():
        return _json_result({"ok": False, "stage": "input", "error": "pdf_url is required"})
    if not target_lang or not target_lang.strip():
        return _json_result({"ok": False, "stage": "input", "error": "target_lang is required"})
    url = pdf_url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return _json_result({"ok": False, "stage": "input", "error": "pdf_url must start with http:// or https://"})
    result = await asyncio.to_thread(
        run_pipeline_url, url, target_lang.strip().lower()
    )
    return _json_result(result)


def main() -> None:
    import sys
    print("pdf-translate MCP server starting (stdio)", file=sys.stderr, flush=True)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
