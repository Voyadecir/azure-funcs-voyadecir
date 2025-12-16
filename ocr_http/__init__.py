import logging
import os
import json
import time
from typing import List, Dict, Any, Tuple, Optional

import azure.functions as func
import urllib.request
import urllib.error
import urllib.parse

logger = logging.getLogger("ocr_http")


def _json_response(body: Dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
    )


def _get_config() -> Dict[str, Any]:
    """
    Read Azure Document Intelligence settings from environment.

    Supports:
    - DOCINTEL_*                    (legacy)
    - AZURE_DOCINTEL_*              (Function App style)
    - AZURE_DOCUMENT_INTELLIGENCE_* (older naming)
    - AZURE_DI_*                    (Render style)

    Defaults to GA API version 2023-07-31, supported in centralus.
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

    return {
        "endpoint": endpoint.rstrip("/"),
        "key": key,
        "api_version": api_version,
        "model_id": model_id,
    }


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


def _normalize_di_content_type(incoming_ct: str, data: bytes) -> str:
    """
    Azure Document Intelligence rejects multipart/form-data.
    It accepts application/pdf, image/*, or application/octet-stream.
    We normalize based on header and (lightly) on magic bytes.
    """
    ct = (incoming_ct or "").split(";")[0].strip().lower()

    # If the client sent the right thing, keep it.
    if ct in ("application/pdf", "application/octet-stream"):
        return ct
    if ct.startswith("image/"):
        return ct

    # If it was multipart or something weird, detect by signature.
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"

    # Safe fallback
    return "application/octet-stream"


def _parse_multipart(body: bytes, content_type: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Minimal multipart/form-data parser (no external deps).
    Returns (file_bytes, filename, part_content_type).
    """
    if not content_type or "multipart/form-data" not in content_type.lower():
        return None, None, None

    # Extract boundary
    lower = content_type.lower()
    boundary_key = "boundary="
    if boundary_key not in lower:
        return None, None, None

    boundary = content_type.split(boundary_key, 1)[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]

    delimiter = ("--" + boundary).encode("utf-8", errors="ignore")
    if delimiter not in body:
        return None, None, None

    parts = body.split(delimiter)
    for p in parts:
        # Skip empties and end marker
        if not p or p in (b"--\r\n", b"--", b"\r\n"):
            continue

        # Trim leading CRLF
        if p.startswith(b"\r\n"):
            p = p[2:]

        # Headers/body separator
        sep = b"\r\n\r\n"
        if sep not in p:
            continue

        header_blob, content_blob = p.split(sep, 1)

        # Drop trailing CRLF and possible end markers
        if content_blob.endswith(b"\r\n"):
            content_blob = content_blob[:-2]
        if content_blob.endswith(b"--"):
            content_blob = content_blob[:-2]

        headers_text = header_blob.decode("utf-8", errors="ignore")
        if "content-disposition" not in headers_text.lower():
            continue
        if "filename=" not in headers_text.lower():
            continue

        filename = None
        part_ct = None

        for line in headers_text.split("\r\n"):
            l = line.strip()
            if l.lower().startswith("content-disposition:"):
                # Example: Content-Disposition: form-data; name="file"; filename="x.pdf"
                if "filename=" in l:
                    fn = l.split("filename=", 1)[1].strip()
                    if fn.startswith('"') and '"' in fn[1:]:
                        filename = fn.split('"', 2)[1]
                    else:
                        filename = fn.strip('"')
            if l.lower().startswith("content-type:"):
                part_ct = l.split(":", 1)[1].strip()

        # Return first file part found
        return content_blob, filename, part_ct

    return None, None, None


def _maybe_parse_json(body: bytes) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Optional future-proof JSON payload parser.
    Accepts shapes like:
      { "bytes_b64": "...", "content_type": "application/pdf" }
      { "data_b64": "...", "contentType": "image/jpeg" }
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return None, None

    b64 = obj.get("bytes_b64") or obj.get("data_b64")
    ct = obj.get("content_type") or obj.get("contentType")
    if not b64:
        return None, None

    import base64
    try:
        data = base64.b64decode(b64, validate=False)
        return data, ct
    except Exception:
        return None, None


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

    normalized_ct = _normalize_di_content_type(content_type, data)

    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": normalized_ct,
    }

    debug_steps.append(f"Incoming Content-Type: {content_type}")
    debug_steps.append(f"Normalized DI Content-Type: {normalized_ct}")
    debug_steps.append(f"Calling analyze: {analyze_url}?api-version={api_version}")

    status, body, resp_headers = _http_post(analyze_url, params=params, headers=headers, data=data)

    if status != 202:
        text_preview = body.decode("utf-8", errors="ignore")[:800]
        debug_steps.append(f"Analyze HTTP {status}: {text_preview}")
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


def _poll_result(operation_url: str, debug_steps: List[str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    cfg = _get_config()
    key = cfg["key"]

    headers = {"Ocp-Apim-Subscription-Key": key}

    for attempt in range(15):
        debug_steps.append(f"Polling result attempt {attempt + 1}")
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
            return None, {"ok": False, "message": "Failed to decode JSON from poll response."}

        status_field = data.get("status") or data.get("analyzeResult", {}).get("status")
        debug_steps.append(f"status={status_field}")

        if status_field in ("succeeded", "Succeeded"):
            return data, None
        if status_field in ("failed", "Failed"):
            return None, {"ok": False, "message": "Analyze operation reported failed.", "raw": data}

        time.sleep(1.0)

    return None, {"ok": False, "message": "Timed out waiting for analyze result."}


def _extract_text(result: Dict[str, Any]) -> Tuple[str, str]:
    full_text = ""
    if "content" in result:
        full_text = result.get("content") or ""
    elif "analyzeResult" in result and isinstance(result["analyzeResult"], dict):
        full_text = result["analyzeResult"].get("content") or ""

    snippet = full_text[:1000]
    return snippet, full_text


def main(req: func.HttpRequest) -> func.HttpResponse:
    debug_steps: List[str] = []

    try:
        incoming_ct = req.headers.get("Content-Type", "")

        raw_body = req.get_body() or b""
        debug_steps.append(f"Received {len(raw_body)} bytes.")
        debug_steps.append(f"Request Content-Type: {incoming_ct}")

        if not raw_body:
            return _json_response(
                {"ok": False, "message": "Request body is empty.", "debug": {"steps": debug_steps}},
                status_code=400,
            )

        # 1) Try JSON/base64 input (future-proof)
        json_bytes, json_ct = _maybe_parse_json(raw_body)
        if json_bytes:
            body = json_bytes
            content_type = json_ct or incoming_ct
            debug_steps.append("Parsed JSON/base64 payload.")
        else:
            # 2) Try multipart/form-data (common browser upload path)
            mp_bytes, mp_name, mp_ct = _parse_multipart(raw_body, incoming_ct)
            if mp_bytes:
                body = mp_bytes
                content_type = mp_ct or incoming_ct
                debug_steps.append(f"Parsed multipart payload. filename={mp_name} part_ct={mp_ct}")
            else:
                # 3) Treat as raw bytes
                body = raw_body
                content_type = incoming_ct or "application/octet-stream"
                debug_steps.append("Using raw request body as document bytes.")

        if not body:
            return _json_response(
                {"ok": False, "message": "No file bytes found in request.", "debug": {"steps": debug_steps}},
                status_code=400,
            )

        # Start analyze
        op_url, err = _analyze_document(body, content_type, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            return _json_response(err, status_code=500)

        # Poll for result
        result, err = _poll_result(op_url, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            return _json_response(err, status_code=500)

        # Extract text
        snippet, full_text = _extract_text(result)

        response_body = {
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
        }

        return _json_response(response_body, status_code=200)

    except Exception as exc:
        logger.exception("Unhandled exception in ocr_http", exc_info=exc)
        return _json_response(
            {
                "ok": False,
                "message": "Unhandled exception in ocr_http.",
                "error": str(exc),
                "debug": {"steps": debug_steps},
            },
            status_code=500,
        )
