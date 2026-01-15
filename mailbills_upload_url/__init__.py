import os
import json
import uuid
import datetime
import azure.functions as func

from azure.storage.blob import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions

CONTAINER = "mailbills"

def _get_account_key_from_conn_string(cs: str) -> str | None:
    # AzureWebJobsStorage is a connection string like:
    # DefaultEndpointsProtocol=https;AccountName=...;AccountKey=...;EndpointSuffix=core.windows.net
    for part in cs.split(";"):
        if part.startswith("AccountKey="):
            return part.split("=", 1)[1]
    return None

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns a short-lived SAS URL that the browser can PUT to (direct-to-Blob upload).
    Response:
      { job_id, blob_url, upload_url, expires_utc }
    """
    try:
        body = {}
        try:
            body = req.get_json() or {}
        except Exception:
            body = {}

        filename = str(body.get("filename") or "upload.pdf")
        content_type = str(body.get("content_type") or "application/octet-stream")

        # Job id becomes the stable handle used throughout the flow
        job_id = str(uuid.uuid4())

        # Make a safe blob path: uploads/<job_id>/<filename>
        safe_name = filename.replace("/", "_").replace("\\", "_").strip() or "upload.pdf"
        blob_name = f"uploads/{job_id}/{safe_name}"

        cs = os.environ["AzureWebJobsStorage"]
        blob_service = BlobServiceClient.from_connection_string(cs)

        # Ensure container exists
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

        # Short-lived SAS token (10 minutes)
        expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)

        sas = generate_blob_sas(
            account_name=account_name,
            container_name=CONTAINER,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True, create=True, write=True),
            expiry=expiry,
        )

        blob_url = f"https://{account_name}.blob.core.windows.net/{CONTAINER}/{blob_name}"
        upload_url = f"{blob_url}?{sas}"

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
