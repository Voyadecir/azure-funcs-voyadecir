import json
import logging
import os
from typing import Dict, Any

import azure.functions as func
import urllib.request
import urllib.error

ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}


def _cors_headers(origin):
    origin_ok = origin if origin in ALLOWED_ORIGINS else ""
    return {
        "Access-Control-Allow-Origin": origin_ok,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _json_response(payload: Dict[str, Any], origin, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        mimetype="application/json",
        headers=_cors_headers(origin),
    )


def _http_post_bytes(url: str, headers: Dict[str, str], body: bytes, timeout: float = 30.0):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def main(req: func.HttpRequest) -> func.HttpResponse:
    origin = req.headers.get("Origin")
    logging.info("mailbills/parse TEST ANALYZE, method=%s, origin=%s", req.method, origin)

    # CORS preflight
    if req.method.upper() == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    # Only POST is supported
    if req.method.upper() != "POST":
        payload = {
            "ok": False,
            "message": "Use POST with PDF or image bytes. (TEST ANALYZE)",
        }
        return _json_response(payload, origin, status_code=200)

    # Read body bytes
    try:
        body_bytes = req.get_body() or b""
    except Exception as e:
        logging.exception("TEST ANALYZE: Failed to read request body")
        payload = {
            "ok": False,
            "message": "Could not read request body.",
            "error": str(e),
        }
        return _json_response(payload, origin, status_code=200)

    length = len(body_bytes)

    # Read env vars (same logic as debug)
    endpoint = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
        or os.getenv("AZURE_DI_ENDPOINT")
        or os.getenv("AZURE_DOCINTEL_ENDPOINT")
        or ""
    )
    key = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        or os.getenv("DOCUMENTINTELLIGENCE_API_KEY")
        or os.getenv("AZURE_DI_API_KEY")
        or os.getenv("AZURE_DOCINTEL_API_KEY")
        or os.getenv("AZURE_DOCINTEL_KEY")
        or ""
    )
    api_version = os.getenv("AZURE_DI_API_VERSION", "2023-07-31")
    model_id = os.getenv("AZURE_DI_MODEL", "prebuilt-read")

    endpoint_stripped = endpoint.rstrip("/") if endpoint else ""
    analyze_url = ""
    if endpoint_stripped:
        analyze_url = (
            f"{endpoint_stripped}/formrecognizer/documentModels/{model_id}:analyze"
            f"?api-version={api_version}"
        )

    debug = {
        "endpoint": endpoint,
        "endpoint_stripped": endpoint_stripped,
        "have_key": bool(key),
        "api_version": api_version,
        "model_id": model_id,
        "analyze_url": analyze_url,
        "received_bytes": length,
    }

    if not endpoint_stripped or not key:
        payload = {
            "ok": False,
            "message": "Missing endpoint or key.",
            "debug": debug,
        }
        return _json_response(payload, origin, status_code=200)

    if not body_bytes:
        payload = {
            "ok": False,
            "message": "Empty request body.",
            "debug": debug,
        }
        return _json_response(payload, origin, status_code=200)

    headers = {
        # For v3.1, binary content is sent directly with an appropriate content type
        "Content-Type": "application/octet-stream",
        "Ocp-Apim-Subscription-Key": key,
    }

    try:
        resp = _http_post_bytes(analyze_url, headers, body_bytes, timeout=30.0)
        status_code = getattr(resp, "status", None) or resp.getcode()
        resp_text = resp.read().decode("utf-8", "ignore")
        op_location = None
        if hasattr(resp, "getheader"):
            op_location = resp.getheader("Operation-Location") or resp.getheader("operation-location")

        payload = {
            "ok": True,
            "message": "Analyze call completed.",
            "http_status": status_code,
            "operation_location": op_location,
            "body_preview": resp_text[:1000],
            "debug": debug,
        }
        return _json_response(payload, origin, status_code=200)

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        payload = {
            "ok": False,
            "message": "Azure OCR analyze HTTPError.",
            "http_status": e.code,
            "body_preview": body[:1000],
            "debug": debug,
        }
        return _json_response(payload, origin, status_code=200)

    except Exception as e:
        logging.exception("Azure OCR analyze general error.")
        payload = {
            "ok": False,
            "message": "Azure OCR analyze general error.",
            "error": str(e),
            "debug": debug,
        }
        return _json_response(payload, origin, status_code=200)
