"""Slim Azure Blob Storage helper for the pdf-translate MCP server.

Mirrors the method names used in the company AzureStorageService
(generate_upload_sas_url / generate_download_sas_url) so this logic ports
cleanly into the production class later. Uses azure-storage-blob directly and
reads its connection string + container name from config (.env).

Account name and key are parsed from the connection string, so only
AZURE_STORAGE_CONNECTION_STRING (and optionally AZURE_STORAGE_CONTAINER_NAME)
need to be set.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

from config import azure_connection_string, azure_container_name

# Azure SDK logs every HTTP request/response at INFO — far too noisy for the
# MCP server's stderr. Keep only warnings and above.
logging.getLogger("azure").setLevel(logging.WARNING)

_service_client: BlobServiceClient | None = None
_containers_ready: set[str] = set()


def _parse_conn_str(conn: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for seg in conn.split(";"):
        key, sep, val = seg.partition("=")
        if sep:
            parts[key.strip()] = val.strip()
    return parts


def _client() -> BlobServiceClient:
    global _service_client
    if _service_client is None:
        _service_client = BlobServiceClient.from_connection_string(azure_connection_string())
    return _service_client


def _account_key() -> str:
    key = _parse_conn_str(azure_connection_string()).get("AccountKey")
    if not key:
        raise RuntimeError("AccountKey not found in AZURE_STORAGE_CONNECTION_STRING")
    return key


def _ensure_container(name: str) -> None:
    """Create the container on first use; ignore if it already exists."""
    if name in _containers_ready:
        return
    try:
        _client().create_container(name)
    except ResourceExistsError:
        pass
    _containers_ready.add(name)


def generate_upload_sas_url(blob_name: str, content_type: str = "application/pdf") -> str:
    """Write-permission SAS URL; HTTP PUT the PDF bytes here (1-hour expiry)."""
    container = azure_container_name()
    _ensure_container(container)
    client = _client()
    sas = generate_blob_sas(
        account_name=client.account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=_account_key(),
        permission=BlobSasPermissions(write=True, create=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        content_type=content_type,
    )
    return f"{client.get_blob_client(container, blob_name).url}?{sas}"


def upload_blob(blob_name: str, data: bytes, content_type: str = "application/pdf") -> None:
    """Upload bytes straight into the container using the account key (server-side).

    Used for the translated output PDF, where the MCP server already holds the
    bytes — no SAS round-trip needed.
    """
    container = azure_container_name()
    _ensure_container(container)
    _client().get_blob_client(container, blob_name).upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )


def generate_download_sas_url(blob_name: str, expiry_hours: int = 1) -> str:
    """Read-permission SAS URL; HTTP GET returns the raw PDF bytes."""
    container = azure_container_name()
    client = _client()
    sas = generate_blob_sas(
        account_name=client.account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=_account_key(),
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
    )
    return f"{client.get_blob_client(container, blob_name).url}?{sas}"
