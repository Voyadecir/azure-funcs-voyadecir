import os
import json
import uuid
import datetime
import azure.functions as func

from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
    ContentSettings,
)

CONTAINER = "mailbills"


def _get_conn_string_value(cs, key_name):
    for part in cs.split(";"):
        if part.startswith(key_name + "="):
            return part.split("=", 1)[1]
    raise RuntimeError(f"Missing {key_name} in AzureWebJobsStorage")


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        filename = body.get("filename") or "document"
        content_type = body.get("content_type") or "application/octet-stream"

        ext = os.path.splitext(filename)[1] or ".bin"
        blob_name = f"{uuid.uuid4().hex}{ext}"

        conn_str = os.environ["AzureWebJobsStorage"]
        account_name = _get_conn_string_value(conn_str, "AccountName")
        account_key = _get_conn_string_value(conn_str, "AccountKey")

        service = BlobServiceClient.from_connection_string(conn_str)
        container = service.get_container_client(CONTAINER)

        try:
            container.create_container()
        except Exception:
            pass  # already exists

        blob = container.get_blob_client(blob_name)

        # Create the blob placeholder (content uploaded later via PUT)
        blob.upload_blob(
            b"",
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

        # Generate SAS AFTER blob exists
        expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=2)

        sas = generate_blob_sas(
            account_name=account_name,
            container_name=CONTAINER,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True, write=True),
            expiry=expiry,
        )

        blob_url = (
            f"https://{account_name}.blob.core.windows.net/"
            f"{CONTAINER}/{blob_name}?{sas}"
        )

        return func.HttpResponse(
            json.dumps({
                "blob_url": blob_url,
                "blob_name": blob_name,
                "expires_utc": expiry.isoformat() + "Z",
                "content_type": content_type,
            }),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({
                "error": "mailbills_upload_url crashed",
                "detail": str(e),
            }),
            status_code=500,
            mimetype="application/json",
        )
