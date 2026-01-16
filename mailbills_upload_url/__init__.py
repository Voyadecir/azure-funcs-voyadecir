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
UPLOAD_TTL_MINUTES = int(os.environ.get("UPLOAD_TTL_MINUTES", "30"))  # safer than 15


def _parse_conn_string(cs: str) -> dict:
    """
    Robust connection string parser (AccountKey contains '=' so never naive-split it).
    """
    out = {}
    for chunk in (cs or "").split(";"):
        if not chunk:
            continue
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k] = v
    return out


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # Optional: accept filename/content_type from client for nicer blob naming/logging
        try:
            body = req.get_json()
        except Exception:
            body = {}

        filename = (body.get("filename") or "upload.bin").strip()
        content_type = (body.get("content_type") or "application/octet-stream").strip()

        # Create a job id that the frontend and later steps can use
        job_id = str(uuid.uuid4())

        # Storage client
        cs = os.environ["AzureWebJobsStorage"]
        cs_parts = _parse_conn_string(cs)
        account_name = cs_parts.get("AccountName")
        account_key = cs_parts.get("AccountKey")

        if not account_name or not account_key:
            raise RuntimeError("AzureWebJobsStorage missing AccountName or AccountKey")

        service = BlobServiceClient.from_connection_string(cs)
        container = service.get_container_client(CONTAINER)

        # Ensure container exists
        try:
            container.create_container()
        except Exception:
            pass

        # Keep the blob name deterministic per job
        # Put under uploads/ so later agents can find it.
        blob_name = f"uploads/{job_id}"
        blob_client = container.get_blob_client(blob_name)

        expiry = datetime.utcnow() + timedelta(minutes=UPLOAD_TTL_MINUTES)

        # 1) Browser upload SAS (PUT BlockBlob): needs create + write
        upload_sas = generate_blob_sas(
            account_name=account_name,
            account_key=account_key,
            container_name=CONTAINER,
            blob_name=blob_name,
            permission=BlobSasPermissions(create=True, write=True),
            expiry=expiry,
        )

        # 2) Read SAS (for DI + private tab download): needs read
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
                    # REQUIRED by your frontend
                    "job_id": job_id,
                    "upload_url": upload_url,
                    "blob_url": blob_url,

                    # Extra debug/helpful fields (won’t break anything)
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
