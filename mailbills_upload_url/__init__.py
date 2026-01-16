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

        blob_name = f"uploads/{job_id}"
        blob_client = cont.get_blob_client(blob_name)

        expiry = datetime.utcnow() + timedelta(seconds=UPLOAD_TTL_SECONDS)

        sas = generate_blob_sas(
            account_name=blob_client.account_name,
            container_name=cont.container_name,
            blob_name=blob_name,
            account_key=os.environ["AzureWebJobsStorage"].split("AccountKey=")[1].split(";")[0],
            permission=BlobSasPermissions(write=True, create=True),
            expiry=expiry,
        )

        upload_url = f"{blob_client.url}?{sas}"
        blob_url = blob_client.url

        return func.HttpResponse(
            json.dumps({
                "job_id": job_id,
                "upload_url": upload_url,
                "blob_url": blob_url,
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
