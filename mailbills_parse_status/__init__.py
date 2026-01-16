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


def _json(status_code: int, payload: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


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


def _load_job(cont, job_id: str):
    job_blob = cont.get_blob_client(f"jobs/{job_id}.json")
    try:
        raw = job_blob.download_blob().readall()
    except Exception:
        # BlobNotFound or transient: treat as pending
        return None, job_blob
    try:
        return json.loads(raw), job_blob
    except Exception:
        return {"status": "failed", "error": "Job record JSON is invalid"}, job_blob


def _get_op_urls(job: dict) -> list:
    # New split-job shape
    di = job.get("di") if isinstance(job, dict) else None
    if isinstance(di, dict):
        op_urls = di.get("op_urls")
        if isinstance(op_urls, list) and op_urls:
            return op_urls

    # Old single-op shape
    op_url = job.get("op_url")
    if isinstance(op_url, str) and op_url.strip():
        return [op_url.strip()]

    return []


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        job_id = (req.params.get("job_id") or "").strip()
        if not job_id:
            return _json(400, {"error": "Missing job_id"})

        cont = _container()
        job, job_blob = _load_job(cont, job_id)

        # If job file not created yet, don't 500. Just say pending.
        if job is None:
            return _json(200, {"status": "pending"})

        # If already done, return cached result
        if job.get("status") == "done":
            try:
                text = cont.get_blob_client(f"results/{job_id}.txt").download_blob().readall().decode("utf-8")
            except Exception:
                text = ""
            return _json(200, {"status": "done", "text": text})

        # If already failed, return that
        if job.get("status") == "failed":
            return _json(200, {"status": "failed", "error": job.get("error")})

        op_urls = _get_op_urls(job)
        if not op_urls:
            job["status"] = "failed"
            job["error"] = "Missing op_url(s)"
            try:
                job_blob.upload_blob(json.dumps(job), overwrite=True)
            except Exception:
                pass
            return _json(200, {"status": "failed", "error": "Missing op_url(s)"})

        key = os.environ["AZURE_DOCINTEL_KEY"]

        part_results = []
        any_failed = False
        all_succeeded = True
        any_running = False

        for idx, op_url in enumerate(op_urls, start=1):
            try:
                r = requests.get(op_url, headers={"Ocp-Apim-Subscription-Key": key}, timeout=30)
            except Exception as ex:
                # Treat request exceptions as "running" (transient) rather than killing the job
                all_succeeded = False
                any_running = True
                part_results.append({
                    "index": idx,
                    "status": "running",
                    "note": f"request_exception: {str(ex)}",
                })
                continue

            if r.status_code != 200:
                all_succeeded = False
                any_running = True
                part_results.append({
                    "index": idx,
                    "status": "running",
                    "note": f"DI returned {r.status_code}",
                })
                continue

            di = r.json()
            st = (di.get("status") or "").lower()

            if st in ("notstarted", "running"):
                all_succeeded = False
                any_running = True
                part_results.append({"index": idx, "status": "running"})
                continue

            if st == "failed":
                any_failed = True
                all_succeeded = False
                part_results.append({
                    "index": idx,
                    "status": "failed",
                    "error": di.get("error") or di,
                })
                continue

            # succeeded
            txt = _extract_text(di) or ""
            part_results.append({
                "index": idx,
                "status": "succeeded",
                "text_len": len(txt),
                "text": txt,
            })

        # Update job progress info for debugging
        job.setdefault("di", {})
        if isinstance(job["di"], dict):
            job["di"]["parts_status"] = [
                {k: v for k, v in pr.items() if k != "text"} for pr in part_results
            ]
            job["di"]["last_status_check"] = int(time.time())

        # If any failed, mark job failed (and store error details)
        if any_failed:
            job["status"] = "failed"
            # store condensed error info
            errs = [pr for pr in part_results if pr.get("status") == "failed"]
            job["error"] = {"parts_failed": errs}
            try:
                job_blob.upload_blob(json.dumps(job), overwrite=True)
            except Exception:
                pass
            return _json(200, {"status": "failed"})

        # If still running, persist job and return running
        if any_running or not all_succeeded:
            job["status"] = "running"
            try:
                job_blob.upload_blob(json.dumps(job), overwrite=True)
            except Exception:
                pass
            return _json(200, {"status": "running"})

        # All succeeded: concatenate text in order
        texts = []
        for pr in part_results:
            if pr.get("status") == "succeeded":
                part_text = pr.get("text") or ""
                # Add a small separator so it's obvious where chunk boundaries were
                texts.append(f"\n\n===== PART {pr.get('index')} =====\n\n{part_text}".strip())

        combined = "\n\n".join(texts).strip()

        # Save combined text result
        cont.upload_blob(f"results/{job_id}.txt", combined, overwrite=True)

        # Mark job done
        job["status"] = "done"
        job["finished_at"] = int(time.time())
        try:
            job_blob.upload_blob(json.dumps(job), overwrite=True)
        except Exception:
            pass

        return _json(200, {"status": "done", "text": combined})

    except Exception as e:
        return _json(500, {"error": "mailbills_parse_status crashed", "detail": str(e)})
