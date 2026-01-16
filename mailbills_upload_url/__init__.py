import os
import json
import uuid
from datetime import datetime, timedelta

import azure.functions as func
from azure.storage.blob import (
    BlobServiceClient,
    BlobSasPermissions,
    generate_blob_sas,
)

CONTAINER = "mailbills"
UPLOAD_TTL_MINUTES = int(os.environ.get("UPLOAD_TTL_MINUTES", "60"))  # 1 hour is sane


def _parse_conn_string(cs: str) -> dict:
    out = {}
    for chunk in (cs or "").split(";"):
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k] = v
    return out


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # Client may send filename/content_type; not required.
        try:
            body = req.get_json()
        except Exception:
            body = {}

        filename = str(body.get("filename") or "upload.bin").strip()
        content_type = str(body.get("content_type") or "application/octet-stream").strip()

        job_id = str(uuid.uuid4())

        cs = os.environ["AzureWebJobsStorage"]
        parts = _parse_conn_string(cs)
        account_name = parts.get("AccountName")
        account_key = parts.get("AccountKey")

        if not account_name or not account_key:
            raise RuntimeError("AzureWebJobsStorage missing AccountName or AccountKey")

        service = BlobServiceClient.from_connection_string(cs)
        container = service.get_container_client(CONTAINER)

        try:
            container.create_container()
        except Exception:
            pass

        # Keep predictable path (your downstream expects uploads/<job_id>)
        blob_name = f"uploads/{job_id}"
        blob_client = container.get_blob_client(blob_name)

        expiry = datetime.utcnow() + timedelta(minutes=UPLOAD_TTL_MINUTES)

        # SAS for browser upload (PUT). Include add=True to avoid weird client behaviors.
        upload_sas = generate_blob_sas(
            account_name=account_name,
            account_key=account_key,
            container_name=CONTAINER,
            blob_name=blob_name,
            permission=BlobSasPermissions(create=True, write=True, add=True),
            expiry=expiry,
        )

        # SAS for DI + verification downloads (read).
        read_sas = generate_blob_sas(
            account_name=account_name,
            account_key=account_key,
            container_name=CONTAINER,
            blob_name=blob_name,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )

        upload_url = f"{blob_client.url}?{upload_sas}"
        blob_url = f"{blob_client.url}?{read_sas}"

        return func.HttpResponse(
            json.dumps(
                {
                    "job_id": job_id,
                    "upload_url": upload_url,
                    "blob_url": blob_url,
                    # Helpful debug fields (won’t break frontend)
                    "blob_name": blob_name,
                    "expires_utc": expiry.isoformat() + "Z",
                    "content_type": content_type,
                    "filename": filename,
                }
            ),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": "mailbills_upload_url crashed", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
