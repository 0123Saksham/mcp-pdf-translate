#!/usr/bin/env python3
"""
pdf-translate MCP server (stdio).

Tools:
  create_upload_url  — mint an Azure Blob upload (write) + download (read) SAS URL
                       pair for a given PDF file name. The client PUTs the PDF to
                       upload_url, then passes download_url to translate_pdf_url.
  translate_pdf_url  — accepts a download URL, server downloads the PDF, runs the
                       full pipeline (extract → translate → check → apply),
                       uploads the result, and returns a download link.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
import asyncio
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
os.chdir(_ROOT)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP

from pipeline import run_pipeline_url
from azure_storage import generate_upload_sas_url, generate_download_sas_url

mcp = FastMCP(
    "pdf-translate",
    instructions=(
        "Translates PDF text layers while preserving layout. Two-step flow: "
        "(1) call create_upload_url with the PDF file name to get an upload_url and "
        "download_url, PUT the PDF bytes to upload_url; "
        "(2) call translate_pdf_url with that download_url and a target language code. "
        "The server downloads the PDF, runs the full pipeline (extract → parallel API "
        "translate → check → apply), uploads the translated PDF, and returns its "
        "download link as 'download_link'."
    ),
)


def _json_result(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _make_sas_urls(blob_name: str) -> tuple[str, str]:
    return generate_upload_sas_url(blob_name), generate_download_sas_url(blob_name)


@mcp.tool()
async def create_upload_url(file_name: str) -> str:
    """
    Create a pre-authorized Azure Blob URL for a PDF the user wants translated.

    Call this FIRST, before translate_pdf_url. Pass only the PDF's file name; the
    server picks a unique blob path and returns two URLs:
      - upload_url:   HTTP PUT the raw PDF bytes here (headers below). Write-only.
      - download_url: pass this to translate_pdf_url afterwards. Read-only.

    The LLM never sends the file bytes through the tool — it PUTs them straight
    to Azure using upload_url.

    Args:
        file_name: The PDF's file name (e.g. "report.pdf"). Used only to name the blob.

    Returns JSON: ok, blob_name, upload_url, download_url, http_method, required_headers.
    """
    name = (file_name or "").strip().replace("\\", "/").split("/")[-1]
    if not name:
        return _json_result({"ok": False, "stage": "input", "error": "file_name is required"})
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    blob_name = f"uploads/{uuid.uuid4().hex}/{name}"
    try:
        upload_url, download_url = await asyncio.to_thread(_make_sas_urls, blob_name)
    except Exception as e:
        return _json_result({"ok": False, "stage": "azure", "error": str(e)})
    return _json_result({
        "ok": True,
        "blob_name": blob_name,
        "upload_url": upload_url,
        "download_url": download_url,
        "http_method": "PUT",
        "required_headers": {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/pdf"},
    })


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
