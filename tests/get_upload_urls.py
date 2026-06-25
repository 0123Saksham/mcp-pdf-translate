#!/usr/bin/env python3
"""Call the create_upload_url MCP tool and print the upload + download URLs.

This mints a real Azure Blob SAS pair (write + read) for a given file name —
exactly what Claude receives from the create_upload_url tool.

Usage:
    python tests/get_upload_urls.py [file_name] [pdf_path_to_actually_upload]

Examples:
    # Just print the URLs for a file name
    python tests/get_upload_urls.py report.pdf

    # Also PUT a real PDF to the upload URL and verify it downloads back
    python tests/get_upload_urls.py 50_pages_french.pdf "D:\\Internship\\skill\\50_pages_french.pdf"
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Run from the MCP root so imports + .env resolve the same way the server does.
_MCP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MCP_ROOT))

from server import create_upload_url  # noqa: E402


async def _main() -> int:
    file_name = sys.argv[1] if len(sys.argv) > 1 else "report.pdf"
    pdf_path = sys.argv[2] if len(sys.argv) > 2 else None

    raw = await create_upload_url(file_name)
    res = json.loads(raw)

    if not res.get("ok"):
        print("create_upload_url FAILED:")
        print(json.dumps(res, indent=2))
        return 1

    print("=== create_upload_url result ===")
    print("blob_name   :", res["blob_name"])
    print("http_method :", res["http_method"])
    print("headers     :", json.dumps(res["required_headers"]))
    print()
    print("UPLOAD URL (PUT the PDF here):")
    print(res["upload_url"])
    print()
    print("DOWNLOAD URL (pass this to translate_pdf_url):")
    print(res["download_url"])

    if not pdf_path:
        print("\n(no pdf_path given — skipped the actual upload/download check)")
        return 0

    import requests

    src = Path(pdf_path)
    if not src.is_file():
        print(f"\nERROR: pdf not found: {src}")
        return 1

    data = src.read_bytes()
    print(f"\nUploading {src.name} ({len(data):,} bytes)...")
    put = requests.put(
        res["upload_url"],
        data=data,
        headers=res["required_headers"],
        timeout=120,
    )
    print("PUT status:", put.status_code)
    if put.status_code not in (200, 201):
        print("PUT body:", put.text[:300])
        return 1

    get = requests.get(res["download_url"], timeout=120)
    ok = get.status_code == 200 and get.content[:4] == b"%PDF" and get.content == data
    print(f"GET status: {get.status_code} | bytes: {len(get.content):,} | round-trip OK: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
