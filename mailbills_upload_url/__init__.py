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
UPLOAD_TTL_SECONDS = 15 * 60  # 15 minutes


def _parse_conn_string(cs: str) -> dict:
    parts = {}
    for chunk in cs.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k] = v
    return parts


def _container():
    cs = os.environ["AzureWebJobsStorage"]
    bs = BlobServiceClient.from_connection_string(cs)
    return bs.get_container_client(CONTAINER)


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        job_id = str(uuid.uuid4())

        cont = _container()
        try:
            cont.create_container()
        except Exception:
            pass

        # We store uploads under uploads/<job_id>
        blob_name = f"uploads/{job_id}"
        blob_client = cont.get_blob_client(blob_name)

        # Pull account name/key from the storage connection string
        cs = os.environ["AzureWebJobsStorage"]
        cs_parts = _parse_conn_string(cs)
        account_name = cs_parts.get("AccountName")
        account_key = cs_parts.get("AccountKey")

        if not account_name or not account_key:
            raise RuntimeError("AzureWebJobsStorage missing AccountName or AccountKey")

        expiry = datetime.utcnow() + timedelta(seconds=UPLOAD_TTL_SECONDS)

        # SAS for browser upload (PUT). Needs create+write.
        upload_sas = generate_blob_sas(
            account_name=account_name,
            container_name=cont.container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(create=True, write=True),
            expiry=expiry,
        )

        # SAS for DI + server-side reads. Needs read.
        read_sas = generate_blob_sas(
            account_name=account_name,
            container_name=cont.container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )

        upload_url = f"{blob_client.url}?{upload_sas}"
        blob_url = f"{blob_client.url}?{read_sas}"  # IMPORTANT: this is what DI must use

        return func.HttpResponse(
            json.dumps(
                {
                    "job_id": job_id,
                    "upload_url": upload_url,
                    "blob_url": blob_url,
                    "expires_utc": expiry.isoformat() + "Z",
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
