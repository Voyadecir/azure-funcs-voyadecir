import os
import json
import time
import requests
import azure.functions as func
from azure.storage.blob import BlobServiceClient

CONTAINER = "mailbills"

# Prefer env-provided values, but keep safe defaults
DI_API_VERSION = os.environ.get("AZURE_DI_API_VERSION", "").strip() or "2024-02-29-preview"
DI_MODEL = os.environ.get("AZURE_DI_MODEL", "").strip() or "prebuilt-read"


def _container():
    cs = os.environ["AzureWebJobsStorage"]
    bs = BlobServiceClient.from_connection_string(cs)
    return bs.get_container_client(CONTAINER)


def _json(status_code: int, payload: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Expected JSON body:
      { "job_id": "...", "blob_url": "https://...SAS..." }
    """
    try:
        # Parse body
        try:
            body = req.get_json()
        except Exception:
            body = None

        if not isinstance(body, dict):
            return _json(400, {"error": "Expected JSON body: { job_id, blob_url }"})

        job_id = str(body.get("job_id") or "").strip()
        blob_url = str(body.get("blob_url") or "").strip()

        if not job_id or not blob_url:
            return _json(400, {"error": "Missing job_id or blob_url"})

        # DI config
        endpoint = os.environ["AZURE_DOCINTEL_ENDPOINT"].rstrip("/")
        key = os.environ["AZURE_DOCINTEL_KEY"]

        headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/json",
        }
        payload = {"urlSource": blob_url}

        # API versions to try (env first, then fallback)
        api_versions = []
        if DI_API_VERSION:
            api_versions.append(DI_API_VERSION)
        if "2023-07-31" not in api_versions:
            api_versions.append("2023-07-31")

        # Routes to try (new + legacy)
        base_routes = [
            "documentintelligence",
            "formrecognizer",
        ]

        # Only use the documentModels :analyze shape (most consistent)
        candidate_urls = []
        for v in api_versions:
            for base in base_routes:
                candidate_urls.append(
                    f"{endpoint}/{base}/documentModels/{DI_MODEL}:analyze?api-version={v}"
                )

        r = None
        used_url = None
        last_status = None
        last_text = ""

        # Try until we get 202 Accepted
        for u in candidate_urls:
            try:
                resp = requests.post(u, headers=headers, json=payload, timeout=30)
            except Exception as ex:
                last_status = None
                last_text = f"Request exception: {str(ex)}"
                continue

            last_status = resp.status_code
            last_text = (resp.text or "")[:1500]

            if resp.status_code == 202:
                r = resp
                used_url = u
                break

        if r is None:
            return _json(
                502,
                {
                    "error": "Document Intelligence start failed",
                    "status": last_status,
                    "detail": last_text,
                    "tried": candidate_urls,
                },
            )

        op_url = r.headers.get("operation-location") or r.headers.get("Operation-Location")
        if not op_url:
            return _json(
                502,
                {
                    "error": "Missing Operation-Location from Document Intelligence",
                    "used_url": used_url,
                    "status": last_status,
                    "detail": last_text,
                },
            )

        # Persist job record
        cont = _container()
        try:
            cont.create_container()
        except Exception:
            # Container exists or we don't have permission; status endpoint will reveal later if needed
            pass

        job_record = {
            "job_id": job_id,
            "status": "running",
            "op_url": op_url,
            "created_at": int(time.time()),
            "blob_url": blob_url,
            "di_start_url": used_url,
            "di_api_version": DI_API_VERSION,
            "di_model": DI_MODEL,
        }

        cont.upload_blob(
            f"jobs/{job_id}.json",
            json.dumps(job_record),
            overwrite=True,
        )

        return _json(200, {"job_id": job_id})

    except Exception as e:
        return _json(500, {"error": "mailbills_parse_start crashed", "detail": str(e)})
