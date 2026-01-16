import os
import io
import json
import time
import math
import requests
import azure.functions as func
from azure.storage.blob import BlobServiceClient, ContentSettings

from pypdf import PdfReader, PdfWriter  # <-- requires `pypdf` in requirements.txt


CONTAINER = "mailbills"

DI_API_VERSION = os.environ.get("AZURE_DI_API_VERSION", "").strip() or "2023-07-31"
DI_MODEL = os.environ.get("AZURE_DI_MODEL", "").strip() or "prebuilt-read"

# Pages per chunk (keep small to avoid DI size limits)
SPLIT_PAGES = int(os.environ.get("OCR_SPLIT_PAGES", "5"))

# Safety cap: max chunks to prevent someone uploading a 900-page PDF and nuking your budget
MAX_CHUNKS = int(os.environ.get("OCR_MAX_CHUNKS", "50"))

# HTTP timeouts
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30"))


def _json(status_code: int, payload: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


def _container():
    cs = os.environ["AzureWebJobsStorage"]
    bs = BlobServiceClient.from_connection_string(cs)
    return bs.get_container_client(CONTAINER)


def _download_bytes_from_sas(url: str) -> bytes:
    # Using SAS URL provided by your upload flow
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.content


def _upload_bytes(cont, blob_name: str, data: bytes, content_type: str = "application/pdf") -> str:
    # Upload chunk PDF to your container
    cont.upload_blob(
        blob_name,
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    # Return *non-SAS* URL for recordkeeping (DI will use SAS generated below)
    return cont.get_blob_client(blob_name).url


def _make_sas_like_url(original_sas_url: str, new_blob_url: str) -> str:
    """
    Reuse the SAS query string from the original upload URL.
    This works because your upload SAS is usually scoped to the container and allows read.
    If your SAS is blob-scoped, this won't work and you'll need to generate SAS server-side.
    """
    if "?" not in original_sas_url:
        return new_blob_url
    qs = original_sas_url.split("?", 1)[1]
    return f"{new_blob_url}?{qs}"


def _build_candidate_di_urls(endpoint: str, api_version: str, model: str) -> list[str]:
    endpoint = endpoint.rstrip("/")
    # Use only :analyze shape (most consistent for documentModels)
    # Try documentintelligence first, then formrecognizer
    return [
        f"{endpoint}/documentintelligence/documentModels/{model}:analyze?api-version={api_version}",
        f"{endpoint}/formrecognizer/documentModels/{model}:analyze?api-version={api_version}",
    ]


def _start_di_analyze(di_urls: list[str], key: str, sas_url: str) -> tuple[str, str]:
    """
    Returns (operation_location, used_di_url)
    """
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/json",
    }
    payload = {"urlSource": sas_url}

    last_status = None
    last_text = ""
    for u in di_urls:
        resp = requests.post(u, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
        last_status = resp.status_code
        last_text = (resp.text or "")[:1500]
        if resp.status_code == 202:
            op_url = resp.headers.get("operation-location") or resp.headers.get("Operation-Location")
            if not op_url:
                raise RuntimeError(f"DI returned 202 but no Operation-Location. used_url={u}")
            return op_url, u

    raise RuntimeError(f"DI start failed. status={last_status} detail={last_text} tried={di_urls}")


def _split_pdf_into_chunks(pdf_bytes: bytes, pages_per_chunk: int) -> list[bytes]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    if total_pages == 0:
        return []

    chunks: list[bytes] = []
    for start in range(0, total_pages, pages_per_chunk):
        writer = PdfWriter()
        end = min(start + pages_per_chunk, total_pages)
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        out = io.BytesIO()
        writer.write(out)
        chunks.append(out.getvalue())
    return chunks


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Expected JSON body:
      { "job_id": "...", "blob_url": "https://...SAS..." }
    """
    try:
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

        # Download the original PDF/image bytes from SAS
        data = _download_bytes_from_sas(blob_url)

        cont = _container()
        try:
            cont.create_container()
        except Exception:
            pass

        # Detect PDF vs image by magic header
        is_pdf = len(data) >= 4 and data[:4] == b"%PDF"

        # If not PDF, we skip splitting and just do one op_url
        part_blob_names: list[str] = []
        part_sas_urls: list[str] = []

        if not is_pdf:
            # Store a copy as a "part" for consistency
            part_name = f"parts/{job_id}/part_001.bin"
            part_url = _upload_bytes(cont, part_name, data, content_type="application/octet-stream")
            part_sas = _make_sas_like_url(blob_url, part_url)
            part_blob_names.append(part_name)
            part_sas_urls.append(part_sas)
        else:
            chunks = _split_pdf_into_chunks(data, SPLIT_PAGES)
            if not chunks:
                return _json(400, {"error": "Uploaded PDF has no pages."})

            if len(chunks) > MAX_CHUNKS:
                return _json(
                    400,
                    {
                        "error": "PDF too long for demo limit",
                        "detail": f"{len(chunks)} chunks would be created (limit {MAX_CHUNKS}).",
                        "hint": "Upload a smaller PDF or increase OCR_MAX_CHUNKS.",
                    },
                )

            # Upload each chunk as its own PDF blob + SAS URL
            digits = int(math.log10(len(chunks))) + 1 if len(chunks) > 0 else 3
            for idx, chunk_bytes in enumerate(chunks, start=1):
                part_name = f"parts/{job_id}/part_{idx:0{digits}d}.pdf"
                part_url = _upload_bytes(cont, part_name, chunk_bytes, content_type="application/pdf")
                part_sas = _make_sas_like_url(blob_url, part_url)
                part_blob_names.append(part_name)
                part_sas_urls.append(part_sas)

        # Start DI for each part
        di_urls = _build_candidate_di_urls(endpoint, DI_API_VERSION, DI_MODEL)

        op_urls: list[str] = []
        used_di_urls: list[str] = []

        for sas_part_url in part_sas_urls:
            op_url, used_url = _start_di_analyze(di_urls, key, sas_part_url)
            op_urls.append(op_url)
            used_di_urls.append(used_url)

        # Persist job record
        job_record = {
            "job_id": job_id,
            "status": "running",
            "created_at": int(time.time()),
            "blob_url": blob_url,

            # Splitting info
            "split": {
                "is_pdf": bool(is_pdf),
                "pages_per_chunk": SPLIT_PAGES if is_pdf else None,
                "parts": part_blob_names,
            },

            # DI info
            "di": {
                "api_version": DI_API_VERSION,
                "model": DI_MODEL,
                "start_urls_tried": di_urls,
                "used_start_urls": used_di_urls,
                "op_urls": op_urls,
            },
        }

        cont.upload_blob(f"jobs/{job_id}.json", json.dumps(job_record), overwrite=True)

        return _json(200, {"job_id": job_id})

    except requests.HTTPError as e:
        # If DI or blob download returns a structured error, surface it cleanly
        return _json(502, {"error": "HTTP error during parse_start", "detail": str(e)})
    except Exception as e:
        return _json(500, {"error": "mailbills_parse_start crashed", "detail": str(e)})
