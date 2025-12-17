import logging
import os
import json
import time
from typing import List, Dict, Any, Tuple, Optional

import azure.functions as func
import requests
from email.parser import BytesParser
from email.policy import default as default_policy

logger = logging.getLogger("ocr_http")


# -----------------------------
# JSON + CORS helpers
# -----------------------------

def _cors_headers() -> Dict[str, str]:
    # Keep allowlist tight. Add staging domains here if needed.
    origin = os.environ.get("CORS_ALLOW_ORIGIN", "https://voyadecir.com")
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-filename",
        "Access-Control-Max-Age": "86400",
    }


def _json_response(body: Dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
        headers=_cors_headers(),
    )


# -----------------------------
# Config
# -----------------------------

def _get_config() -> Dict[str, str]:
    endpoint = (
        os.environ.get("AZURE_DOCINTEL_ENDPOINT")
        or os.environ.get("AZURE_DI_ENDPOINT")
        or ""
    )
    key = (
        os.environ.get("AZURE_DOCINTEL_KEY")
        or os.environ.get("AZURE_DI_API_KEY")
        or os.environ.get("AZURE_DI_KEY")
        or ""
    )
    api_version = (
        os.environ.get("AZURE_DOCINTEL_API_VERSION")
        or os.environ.get("AZURE_DI_API_VERSION")
        # GA version that works broadly in centralus.
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


# -----------------------------
# Upload parsing + MIME sniffing
# -----------------------------

def _sniff_mime(data: bytes, fallback: str = "application/octet-stream") -> str:
    if not data:
        return fallback
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return fallback


def _extract_file_from_request(req: func.HttpRequest, debug_steps: List[str]) -> Tuple[bytes, str]:
    # Supports:
    #   - raw bytes POST (recommended; used by current frontend)
    #   - multipart/form-data (some browsers/clients)
    # Returns: (file_bytes, mime)
    content_type = (req.headers.get("Content-Type") or "").strip()
    body = req.get_body() or b""

    debug_steps.append(f"Incoming Content-Type: {content_type or '∅'}")
    debug_steps.append(f"Incoming body bytes: {len(body)}")

    # Multipart upload?
    if content_type.lower().startswith("multipart/form-data"):
        debug_steps.append("Parsing multipart/form-data upload")

        raw = (
            b"Content-Type: " + content_type.encode("utf-8") +
            b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        msg = BytesParser(policy=default_policy).parsebytes(raw)

        for part in msg.iter_parts():
            filename = part.get_filename()
            payload = part.get_payload(decode=True)
            if filename and payload:
                mime = (part.get_content_type() or "").strip()
                if not mime or mime in ("application/octet-stream", "binary/octet-stream"):
                    mime = _sniff_mime(payload, "application/octet-stream")
                debug_steps.append(f"Found multipart file: {filename} ({len(payload)} bytes) mime={mime}")
                return payload, mime

        raise ValueError("No file found in multipart upload.")

    # Raw bytes path
    mime = content_type.split(";")[0].strip().lower() if content_type else ""
    if not mime or mime in ("application/octet-stream", "binary/octet-stream", "text/plain"):
        sniffed = _sniff_mime(body, "application/octet-stream")
        debug_steps.append(f"MIME normalized: {mime or '∅'} -> {sniffed}")
        mime = sniffed

    return body, mime


# -----------------------------
# Document Intelligence (Read)
# -----------------------------

def _analyze_document(data: bytes, content_type: str, debug_steps: List[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    cfg = _get_config()
    endpoint = cfg["endpoint"]
    key = cfg["key"]
    api_version = cfg["api_version"]
    model_id = cfg["model_id"]

    if not endpoint or not key:
        return None, {
            "ok": False,
            "message": "Azure Document Intelligence endpoint or key is not configured.",
        }

    # DI expects specific content types.
    if content_type not in ("application/pdf", "image/jpeg", "image/png"):
        debug_steps.append(f"Rejecting unsupported content_type={content_type}")
        return None, {
            "ok": False,
            "message": f"UnsupportedMediaType: {content_type}. Upload a PDF, JPG, or PNG.",
        }

    # NOTE: use /formrecognizer for the GA 2023-07-31 API in centralus.
    analyze_url = f"{endpoint}/formrecognizer/documentModels/{model_id}:analyze"
    params = {"api-version": api_version}

    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": content_type,
    }

    debug_steps.append(f"Calling analyze: {analyze_url}?api-version={api_version}")

    try:
        r = requests.post(analyze_url, params=params, headers=headers, data=data, timeout=30)
    except Exception as exc:
        return None, {
            "ok": False,
            "message": "Analyze call failed (network error).",
            "error": str(exc),
        }

    if r.status_code >= 400:
        preview = (r.text or "")[:2000]
        debug_steps.append(f"Analyze HTTP {r.status_code}: {preview[:200]}")
        return None, {
            "ok": False,
            "message": f"Analyze call failed with HTTP {r.status_code}.",
            "body_preview": preview,
        }

    op_url = r.headers.get("operation-location") or r.headers.get("Operation-Location")
    if not op_url:
        debug_steps.append("Analyze succeeded but no Operation-Location header present")
        return None, {
            "ok": False,
            "message": "Analyze call succeeded but Operation-Location header was missing.",
        }

    debug_steps.append(f"Operation-Location: {op_url}")
    return op_url, None


def _poll_operation(op_url: str, debug_steps: List[str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    cfg = _get_config()
    key = cfg["key"]

    poll_attempts = int(float(os.environ.get("AZURE_DI_POLL_ATTEMPTS", "15")))
    poll_wait = float(os.environ.get("AZURE_DI_MAX_POLL_WAIT", "2.0"))

    headers = {"Ocp-Apim-Subscription-Key": key}

    last_preview = None
    for i in range(poll_attempts):
        time.sleep(poll_wait)
        try:
            r = requests.get(op_url, headers=headers, timeout=30)
        except Exception as exc:
            return None, {
                "ok": False,
                "message": "Poll failed (network error).",
                "error": str(exc),
            }

        if r.status_code >= 400:
            preview = (r.text or "")[:2000]
            debug_steps.append(f"Poll HTTP {r.status_code}: {preview[:200]}")
            return None, {
                "ok": False,
                "message": f"Poll failed with HTTP {r.status_code}.",
                "body_preview": preview,
            }

        data = r.json()
        status = (data.get("status") or "").lower()
        last_preview = json.dumps(data)[:500]
        debug_steps.append(f"Poll {i+1}/{poll_attempts}: status={status}")

        if status == "succeeded":
            return data, None
        if status == "failed":
            return None, {
                "ok": False,
                "message": "Analyze failed.",
                "body_preview": json.dumps(data)[:2000],
            }

    return None, {
        "ok": False,
        "message": "Analyze polling timed out.",
        "body_preview": (last_preview or "")[:2000],
    }


def _extract_text(result: Dict[str, Any]) -> str:
    analyze = result.get("analyzeResult") or {}
    pages = analyze.get("pages") or []
    out: List[str] = []
    for p in pages:
        for line in (p.get("lines") or []):
            c = line.get("content")
            if c:
                out.append(c)
    return "\n".join(out).strip()


# -----------------------------
# Azure Functions entrypoint
# -----------------------------

def main(req: func.HttpRequest) -> func.HttpResponse:
    debug_steps: List[str] = []
    try:
        if req.method == "OPTIONS":
            return func.HttpResponse("", status_code=204, headers=_cors_headers())

        file_bytes, mime = _extract_file_from_request(req, debug_steps)
        if not file_bytes:
            return _json_response(
                {"ok": False, "message": "Request body is empty.", "debug": {"steps": debug_steps}},
                status_code=400,
            )

        op_url, err = _analyze_document(file_bytes, mime, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            status = 415 if (err.get("message") or "").startswith("UnsupportedMediaType") else 500
            return _json_response(err, status_code=status)

        result, err = _poll_operation(op_url, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            return _json_response(err, status_code=500)

        text = _extract_text(result or {})
        if not text:
            return _json_response(
                {"ok": False, "message": "OCR returned no text. Try a clearer photo or PDF.", "debug": {"steps": debug_steps}},
                status_code=200,
            )

        return _json_response({"ok": True, "text": text, "debug": {"steps": debug_steps}}, status_code=200)

    except ValueError as ve:
        logger.exception("Bad request in ocr_http", exc_info=ve)
        return _json_response({"ok": False, "message": str(ve), "debug": {"steps": debug_steps}}, status_code=400)
    except Exception as exc:
        logger.exception("Unhandled exception in ocr_http", exc_info=exc)
        return _json_response(
            {"ok": False, "message": "Unhandled exception in ocr_http.", "error": str(exc), "debug": {"steps": debug_steps}},
            status_code=500,
        )
