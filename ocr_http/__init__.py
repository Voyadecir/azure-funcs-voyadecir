import logging
import os
import json
import time
import re
from io import BytesIO
from typing import List, Dict, Any, Tuple, Optional

import azure.functions as func
import urllib.request
import urllib.error
import urllib.parse

logger = logging.getLogger("ocr_http")


# -------------------------
# CORS
# -------------------------
def _cors_headers(req: func.HttpRequest) -> Dict[str, str]:
    """
    Allow Voyadecir sites by default. You can override with env var:
      CORS_ALLOWED_ORIGINS="https://voyadecir.com,https://www.voyadecir.com"
    """
    allowed = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://voyadecir.com,https://www.voyadecir.com",
    )
    allowlist = [x.strip() for x in allowed.split(",") if x.strip()]
    origin = req.headers.get("Origin", "")
    allow_origin = origin if origin in allowlist else (allowlist[0] if allowlist else "*")

    return {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
        "Access-Control-Max-Age": "86400",
    }


def _json_response(req: func.HttpRequest, body: Dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    """Return JSON with CORS headers."""
    headers = {"Content-Type": "application/json"}
    headers.update(_cors_headers(req))
    return func.HttpResponse(
        body=json.dumps(body),
        status_code=status_code,
        headers=headers,
        mimetype="application/json",
    )


# -------------------------
# Config
# -------------------------
def _get_config() -> Dict[str, Any]:
    """
    Azure Document Intelligence settings from environment.

    Supports:
    - DOCINTEL_*                    (legacy)
    - AZURE_DOCINTEL_*              (Function App style)
    - AZURE_DOCUMENT_INTELLIGENCE_* (older naming)
    - AZURE_DI_*                    (Render style)

    Defaults:
    - api_version = 2023-07-31 (GA)
    - model_id = prebuilt-read
    """
    endpoint = (
        os.environ.get("DOCINTEL_ENDPOINT")
        or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or os.environ.get("AZURE_DOCINTEL_ENDPOINT")
        or os.environ.get("AZURE_DI_ENDPOINT")
        or ""
    )
    key = (
        os.environ.get("DOCINTEL_KEY")
        or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        or os.environ.get("AZURE_DOCINTEL_KEY")
        or os.environ.get("AZURE_DI_API_KEY")
        or ""
    )
    api_version = (
        os.environ.get("DOCINTEL_API_VERSION")
        or os.environ.get("AZURE_DOCINTEL_API_VERSION")
        or os.environ.get("AZURE_DI_API_VERSION")
        or "2023-07-31"
    )
    model_id = (
        os.environ.get("DOCINTEL_MODEL_ID")
        or os.environ.get("AZURE_DOCINTEL_MODEL_ID")
        or os.environ.get("AZURE_DI_MODEL")
        or "prebuilt-read"
    )

    poll_attempts = int(os.environ.get("AZURE_DI_POLL_ATTEMPTS", "15"))
    poll_sleep = float(os.environ.get("AZURE_DI_MAX_POLL_WAIT", "1.0"))

    return {
        "endpoint": endpoint.rstrip("/"),
        "key": key,
        "api_version": api_version,
        "model_id": model_id,
        "poll_attempts": poll_attempts,
        "poll_sleep": poll_sleep,
    }


# -------------------------
# HTTP helpers (urllib)
# -------------------------
def _http_post(
    url: str,
    params: Dict[str, str],
    headers: Dict[str, str],
    data: bytes,
    timeout: float = 30.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read()
            resp_headers = dict(resp.getheaders())
            return status, body, resp_headers
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body, dict(e.headers or {})
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP POST failed: {e}") from e


def _http_get(
    url: str,
    headers: Dict[str, str],
    timeout: float = 30.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read()
            resp_headers = dict(resp.getheaders())
            return status, body, resp_headers
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body, dict(e.headers or {})
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP GET failed: {e}") from e


# -------------------------
# Upload parsing
# -------------------------
def _infer_content_type(data: bytes) -> str:
    if not data:
        return "application/octet-stream"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _parse_multipart(body: bytes, content_type: str, debug_steps: List[str]) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Minimal multipart/form-data parser:
    Returns (file_bytes, file_content_type, filename)
    """
    m = re.search(r"boundary=(.+)", content_type)
    if not m:
        debug_steps.append("multipart: missing boundary in Content-Type")
        return None, None, None

    boundary = m.group(1).strip()
    # boundary may be quoted
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]

    boundary_bytes = ("--" + boundary).encode("utf-8")
    parts = body.split(boundary_bytes)

    debug_steps.append(f"multipart: parts={len(parts)} boundary={boundary[:40]}...")

    for p in parts:
        p = p.strip(b"\r\n")
        if not p or p == b"--":
            continue
        if p.endswith(b"--"):
            p = p[:-2].strip(b"\r\n")

        # Split headers + data
        header_end = p.find(b"\r\n\r\n")
        if header_end == -1:
            continue

        header_blob = p[:header_end].decode("utf-8", errors="ignore")
        data_blob = p[header_end + 4 :]

        # Look for Content-Disposition: form-data; name="file"; filename="x.pdf"
        if "content-disposition" not in header_blob.lower():
            continue

        # Prefer a part that includes filename
        if "filename=" not in header_blob.lower():
            continue

        filename = None
        ct = None

        # filename
        fnm = re.search(r'filename="([^"]+)"', header_blob, re.IGNORECASE)
        if fnm:
            filename = fnm.group(1)

        # content-type header inside part
        ctm = re.search(r"content-type:\s*([^\r\n]+)", header_blob, re.IGNORECASE)
        if ctm:
            ct = ctm.group(1).strip()

        # Strip trailing CRLF that belongs to multipart framing
        data_blob = data_blob.rstrip(b"\r\n")

        if data_blob:
            debug_steps.append(f"multipart: extracted file bytes={len(data_blob)} filename={filename} ct={ct}")
            return data_blob, ct, filename

    debug_steps.append("multipart: no file part found (expected filename=...)")
    return None, None, None


def _extract_upload(req: func.HttpRequest, debug_steps: List[str]) -> Tuple[Optional[bytes], Optional[str], Optional[str], Optional[Dict[str, Any]]]:
    """
    Returns (bytes, content_type, filename, err)
    Accepts:
      - raw body (pdf/image)
      - multipart/form-data
    """
    body = req.get_body() or b""
    if not body:
        return None, None, None, {"ok": False, "message": "Request body is empty."}

    ct = (req.headers.get("Content-Type") or "").lower()
    debug_steps.append(f"request Content-Type={ct}")

    if ct.startswith("multipart/form-data"):
        file_bytes, file_ct, filename = _parse_multipart(body, req.headers.get("Content-Type", ""), debug_steps)
        if not file_bytes:
            return None, None, None, {
                "ok": False,
                "message": "No file found in multipart/form-data. Upload must include a file.",
            }
        final_ct = (file_ct or _infer_content_type(file_bytes))
        return file_bytes, final_ct, filename, None

    # raw bytes
    raw_ct = req.headers.get("Content-Type") or ""
    final_ct = raw_ct if raw_ct and not raw_ct.lower().startswith("application/json") else _infer_content_type(body)
    return body, final_ct, None, None


# -------------------------
# Azure DI calls
# -------------------------
def _analyze_document(
    data: bytes,
    content_type: str,
    debug_steps: List[str],
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    cfg = _get_config()
    endpoint = cfg["endpoint"]
    key = cfg["key"]
    api_version = cfg["api_version"]
    model_id = cfg["model_id"]

    if not endpoint or not key:
        debug_steps.append("Missing endpoint or key in environment.")
        return None, {
            "ok": False,
            "message": "Azure Document Intelligence endpoint or key is not configured.",
        }

    analyze_url = f"{endpoint}/formrecognizer/documentModels/{model_id}:analyze"
    params = {"api-version": api_version}

    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": content_type or "application/octet-stream",
    }

    debug_steps.append(f"Calling analyze: {analyze_url}?api-version={api_version} ct={headers['Content-Type']} bytes={len(data)}")

    status, body, resp_headers = _http_post(
        analyze_url, params=params, headers=headers, data=data
    )

    if status != 202:
        text_preview = body.decode("utf-8", errors="ignore")[:800]
        debug_steps.append(f"Analyze HTTP {status}: {text_preview}")
        # IMPORTANT: return ok:false but NOT a 500 to the browser (prevents noisy console + fetch errors)
        return None, {
            "ok": False,
            "message": f"Analyze call failed with HTTP {status}.",
            "body_preview": text_preview,
        }

    op_location = (
        resp_headers.get("operation-location")
        or resp_headers.get("Operation-Location")
        or resp_headers.get("Operation-location")
    )
    debug_steps.append(f"operation-location: {op_location}")
    if not op_location:
        return None, {
            "ok": False,
            "message": "Analyze call did not return operation-location header.",
        }

    return op_location, None


def _poll_result(
    operation_url: str,
    debug_steps: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    cfg = _get_config()
    key = cfg["key"]
    attempts = cfg["poll_attempts"]
    sleep_s = cfg["poll_sleep"]

    headers = {"Ocp-Apim-Subscription-Key": key}

    for attempt in range(attempts):
        debug_steps.append(f"Polling result attempt {attempt + 1}/{attempts}")

        status, body, _ = _http_get(operation_url, headers=headers)

        if status != 200:
            text_preview = body.decode("utf-8", errors="ignore")[:800]
            debug_steps.append(f"Poll HTTP {status}: {text_preview}")
            return None, {
                "ok": False,
                "message": f"Poll failed with HTTP {status}.",
                "body_preview": text_preview,
            }

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            debug_steps.append(f"JSON decode error: {e}")
            return None, {
                "ok": False,
                "message": "Failed to decode JSON from poll response.",
            }

        status_field = data.get("status") or data.get("analyzeResult", {}).get("status")
        debug_steps.append(f"status={status_field}")

        if status_field in ("succeeded", "Succeeded"):
            return data, None
        if status_field in ("failed", "Failed"):
            return None, {
                "ok": False,
                "message": "Analyze operation reported failed.",
                "raw": data,
            }

        time.sleep(sleep_s)

    return None, {
        "ok": False,
        "message": "Timed out waiting for analyze result.",
    }


def _extract_text(result: Dict[str, Any]) -> Tuple[str, str]:
    full_text = ""
    if "content" in result:
        full_text = result.get("content") or ""
    elif "analyzeResult" in result and isinstance(result["analyzeResult"], dict):
        full_text = result["analyzeResult"].get("content") or ""

    snippet = full_text[:1000]
    return snippet, full_text


# -------------------------
# Azure Function entrypoint
# -------------------------
def main(req: func.HttpRequest) -> func.HttpResponse:
    debug_steps: List[str] = []

    # CORS preflight
    if req.method and req.method.upper() == "OPTIONS":
        return func.HttpResponse(status_code=204, headers=_cors_headers(req))

    try:
        file_bytes, file_ct, filename, err = _extract_upload(req, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            return _json_response(req, err, status_code=400)

        debug_steps.append(f"upload: filename={filename} ct={file_ct} bytes={len(file_bytes or b'')}")

        # Start analyze
        op_url, err = _analyze_document(file_bytes or b"", file_ct or "application/octet-stream", debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            # Return 200 to avoid browser console "500", while still signaling failure
            return _json_response(req, err, status_code=200)

        # Poll
        result, err = _poll_result(op_url, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            return _json_response(req, err, status_code=200)

        snippet, full_text = _extract_text(result)

        if not (full_text or "").strip():
            return _json_response(
                req,
                {
                    "ok": False,
                    "message": "OCR returned no text. Try a clearer photo or PDF.",
                    "debug": {"steps": debug_steps, "operation_url": op_url},
                },
                status_code=200,
            )

        return _json_response(
            req,
            {
                "ok": True,
                "message": "OCR succeeded.",
                "ocr_text_snippet": snippet,
                "ocr_text": full_text,
                "summary_en": "",
                "summary_translated": "",
                "fields": {
                    "amount_due": {"value": "", "confidence": 0.0},
                    "due_date": {"value": "", "confidence": 0.0},
                    "account_number": {"value": "", "confidence": 0.0},
                    "sender": {"value": "", "confidence": 0.0},
                    "service_address": {"value": "", "confidence": 0.0},
                },
                "debug": {"steps": debug_steps, "operation_url": op_url},
            },
            status_code=200,
        )

    except Exception as exc:
        logger.exception("Unhandled exception in ocr_http", exc_info=exc)
        return _json_response(
            req,
            {
                "ok": False,
                "message": "Unhandled exception in ocr_http.",
                "error": str(exc),
                "debug": {"steps": debug_steps},
            },
            status_code=500,
        )
