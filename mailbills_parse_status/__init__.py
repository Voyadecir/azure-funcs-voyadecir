import os
import json
import time
import requests
import azure.functions as func
from azure.storage.blob import BlobServiceClient

CONTAINER = "mailbills"

def _container():
    cs = os.environ["AzureWebJobsStorage"]
    bs = BlobServiceClient.from_connection_string(cs)
    return bs.get_container_client(CONTAINER)

def _extract_text(di: dict) -> str:
    # Best-effort across DI response shapes
    content = di.get("content")
    if isinstance(content, str) and content.strip():
        return content

    ar = di.get("analyzeResult") or {}
    content2 = ar.get("content")
    if isinstance(content2, str) and content2.strip():
        return content2

    pages = ar.get("pages") or []
    lines = []
    for p in pages:
        for ln in (p.get("lines") or []):
            c = ln.get("content")
            if c:
                lines.append(c)
    return "\n".join(lines)

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        job_id = (req.params.get("job_id") or "").strip()
        if not job_id:
            return func.HttpResponse(
                json.dumps({"error": "Missing job_id"}),
                status_code=400,
                mimetype="application/json",
            )

        cont = _container()
        job_blob = cont.get_blob_client(f"jobs/{job_id}.json")
        job = json.loads(job_blob.download_blob().readall())

        if job.get("status") == "done":
            text = cont.get_blob_client(f"results/{job_id}.txt").download_blob().readall().decode("utf-8")
            return func.HttpResponse(
                json.dumps({"status": "done", "text": text}),
                status_code=200,
                mimetype="application/json",
            )

        op_url = job.get("op_url")
        if not op_url:
            return func.HttpResponse(
                json.dumps({"status": "failed", "error": "Missing op_url"}),
                status_code=200,
                mimetype="application/json",
            )

        key = os.environ["AZURE_DOCINTEL_KEY"]
        r = requests.get(op_url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=30)

        if r.status_code != 200:
            return func.HttpResponse(
                json.dumps({"status": "running", "note": f"DI returned {r.status_code}"}),
                status_code=200,
                mimetype="application/json",
            )

        di = r.json()
        st = (di.get("status") or "").lower()

        if st in ("notstarted", "running"):
            return func.HttpResponse(json.dumps({"status": "running"}), status_code=200, mimetype="application/json")

        if st == "failed":
            job["status"] = "failed"
            job["error"] = di.get("error") or di
            job_blob.upload_blob(json.dumps(job), overwrite=True)
            return func.HttpResponse(json.dumps({"status": "failed"}), status_code=200, mimetype="application/json")

        # succeeded
        text = _extract_text(di) or ""
        cont.upload_blob(f"results/{job_id}.txt", text, overwrite=True)

        job["status"] = "done"
        job["finished_at"] = int(time.time())
        job_blob.upload_blob(json.dumps(job), overwrite=True)

        return func.HttpResponse(
            json.dumps({"status": "done", "text": text}),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": "mailbills_parse_status crashed", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
