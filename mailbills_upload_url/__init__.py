import os
import json
import uuid
import datetime
import azure.functions as func

from azure.storage.blob import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions

CONTAINER = "mailbills"

def _get_account_key_from_conn_string(cs):
    for part in cs.split(";"):
        if part.startswith("AccountKey="):
            return part.split("=", 1)[1]
    return None

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = {}
        try:
            body = req.get_json() or {}
        except Exception:
            body = {}

        filename = str(body.get("filename") or "upload.pdf")
        content_type = str(body.get("content_type") or "application/octet-stream")

        job_id = str(uuid.uuid4())

        safe_name = filename.replace("/", "_").replace("\\", "_").strip() or "upload.pdf"
        blob_name = "uploads/{}/{}".format(job_id, safe_name)

        cs = os.environ["AzureWebJobsStorage"]
        blob_service = BlobServiceClient.from_connection_string(cs)

        container = blob_service.get_container_client(CONTAINER)
        try:
            container.create_container()
        except Exception:
            pass

        account_name = blob_service.account_name
        account_key = _get_account_key_from_conn_string(cs)
        if not account_key:
            return func.HttpResponse(
                json.dumps({"error": "Could not read AccountKey from AzureWebJobsStorage"}),
                status_code=500,
                mimetype="application/json",
            )

        expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)

        sas = generate_blob_sas(
            account_name=account_name,
            container_name=CONTAINER,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True, create=True, write=True),
            expiry=expiry,
        )

        blob_url = "https://{}.blob.core.windows.net/{}/{}".format(account_name, CONTAINER, blob_name)
        upload_url = "{}?{}".format(blob_url, sas)

        # IMPORTANT: return the exact keys the website expects
        return func.HttpResponse(
            json.dumps({
                "job_id": job_id,
                "blob_url": blob_url,
                "upload_url": upload_url,
                "expires_utc": expiry.isoformat() + "Z",
                "content_type": content_type
            }),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": "mailbills_upload_url crashed", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
