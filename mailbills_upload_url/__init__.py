import os
import json
import uuid
import datetime
import azure.functions as func

from azure.storage.blob import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions

CONTAINER = "mailbills"

def _get_conn_string_value(cs, key_name):
    # Parses connection string pairs like "AccountName=...;AccountKey=...;"
    for part in cs.split(";"):
        if part.startswith(key_name + "="):
            return part.split("=", 1)[1]
    return None

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns a short-lived SAS URL the browser can PUT to (direct-to-Blob upload).

    Response JSON (exact keys the website expects):
      {
        "job_id": "...",
        "blob_url": "https://<acct>.blob.core.windows.net/mailbills/uploads/<job_id>/<filename>",
        "upload_url": "blob_url?<sas>",
        "expires_utc": "..."
      }
    """
    try:
        # Read JSON input (filename/content_type)
        body = {}
        try:
            body = req.get_json() or {}
        except Exception:
            body = {}

        filename = str(body.get("filename") or "upload.pdf")
        content_type = str(body.get("content_type") or "application/octet-stream")

        # Generate job_id used throughout the pipeline
        job_id = str(uuid.uuid4())

        # Safe blob name: uploads/<job_id>/<filename>
        safe_name = filename.replace("/", "_").replace("\\", "_").strip() or "upload.pdf"
        blob_name = "uploads/{}/{}".format(job_id, safe_name)

        # Use the same storage account your Function App already uses
        cs = os.environ["AzureWebJobsStorage"]
        account_name = _get_conn_string_value(cs, "AccountName")
        account_key = _get_conn_string_value(cs, "AccountKey")

        if not account_name or not account_key:
            return func.HttpResponse(
                json.dumps({"error": "AzureWebJobsStorage missing AccountName/AccountKey"}),
                status_code=500,
                mimetype="application/json",
            )

        # Ensure container exists
        blob_service = BlobServiceClient.from_connection_string(cs)
        container = blob_service.get_container_client(CONTAINER)
        try:
            container.create_container()
        except Exception:
            pass

        # Create SAS for PUT upload (write/create) + optional read
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

        # Return EXACT keys expected by your frontend
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
        # Return JSON so failures are visible (no silent empty 500)
        return func.HttpResponse(
            json.dumps({"error": "mailbills_upload_url crashed", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
