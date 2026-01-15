import os
import json
import time
import requests
import azure.functions as func
from azure.storage.blob import BlobServiceClient

CONTAINER = "mailbills"
DI_API_VERSION = os.environ.get("AZURE_DI_API_VERSION", "2024-02-29-preview")
DI_MODEL = os.environ.get("AZURE_DI_MODEL", "prebuilt-read")

def _container():
    cs = os.environ["AzureWebJobsStorage"]
    bs = BlobServiceClient.from_connection_string(cs)
    return bs.get_container_client(CONTAINER)

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Expected JSON body:
      { "job_id": "...", "blob_url": "https://...SAS..." }

    Note: blob_url SHOULD include SAS so Document Intelligence can fetch it.
    """
    try:
        try:
            body = req.get_json()
        except Exception:
            body = None

        if not isinstance(body, dict):
            return func.HttpResponse(
                json.dumps({"error": "Expected JSON body: { job_id, blob_url }"}),
                status_code=400,
                mimetype="application/json",
            )

        job_id = str(body.get("job_id") or "").strip()
        blob_url = str(body.get("blob_url") or "").strip()

        if not job_id or not blob_url:
            return func.HttpResponse(
                json.dumps({"error": "Missing job_id or blob_url"}),
                status_code=400,
                mimetype="application/json",
            )

        endpoint = os.environ["AZURE_DOCINTEL_ENDPOINT"].rstrip("/")
        key = os.environ["AZURE_DOCINTEL_KEY"]

        # Start DI async job using URL source (no big PDF bytes through Functions)
        url = f"{endpoint}/documentintelligence/documentModels/{DI_MODEL}:analyze?api-version={DI_API_VERSION}"
        headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/json",
        }
        payload = {"urlSource": blob_url}

        r = requests.post(url, headers=headers, json=payload, timeout=30)

        if r.status_code != 202:
            return func.HttpResponse(
                json.dumps({
                    "error": "Document Intelligence start failed",
                    "status": r.status_code,
                    "detail": (r.text or "")[:1500],
                }),
                status_code=502,
                mimetype="application/json",
            )

        op_url = r.headers.get("operation-location") or r.headers.get("Operation-Location")
        if not op_url:
            return func.HttpResponse(
                json.dumps({"error": "Missing Operation-Location from Document Intelligence"}),
                status_code=502,
                mimetype="application/json",
            )

        cont = _container()
        try:
            cont.create_container()
        except Exception:
            pass

        # Save job record (status endpoint uses this)
        job_record = {
            "job_id": job_id,
            "status": "running",
            "op_url": op_url,
            "created_at": int(time.time()),
            "blob_url": blob_url
        }

        cont.upload_blob(f"jobs/{job_id}.json", json.dumps(job_record), overwrite=True)

        return func.HttpResponse(
            json.dumps({"job_id": job_id}),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": "mailbills_parse_start crashed", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
