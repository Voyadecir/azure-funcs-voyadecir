import json
import logging
import os

import azure.functions as func

ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}


def _cors_headers(origin):
    origin_ok = origin if origin in ALLOWED_ORIGINS else ""
    return {
        "Access-Control-Allow-Origin": origin_ok,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def main(req: func.HttpRequest) -> func.HttpResponse:
    origin = req.headers.get("Origin")
    logging.info("ENV DEBUG mailbills/parse, method=%s, origin=%s", req.method, origin)

    # CORS preflight
    if req.method.upper() == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    # Only POST is supported (keep 200 so frontend doesn't explode)
    if req.method.upper() != "POST":
        payload = {
            "ok": False,
            "message": "Use POST with PDF or image bytes. (ENV DEBUG)",
        }
        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json",
            headers=_cors_headers(origin),
        )

    try:
        body = req.get_body() or b""
        length = len(body)
    except Exception as e:
        logging.exception("ENV DEBUG: Failed to read request body")
        payload = {
            "ok": False,
            "message": "ENV DEBUG: Could not read request body.",
            "error": str(e),
        }
        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json",
            headers=_cors_headers(origin),
        )

    # Read env vars exactly as our OCR code would
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

    form_url = ""
    docintel_url = ""
    if endpoint_stripped:
        form_url = (
            f"{endpoint_stripped}/formrecognizer/documentModels/{model_id}:analyze"
            f"?api-version={api_version}"
        )
        docintel_url = (
            f"{endpoint_stripped}/documentintelligence/documentModels/{model_id}:analyze"
            f"?api-version={api_version}"
        )

    payload = {
        "ok": True,
        "message": "ENV DEBUG: function alive, showing configuration only.",
        "received_bytes": length,
        "config": {
            "endpoint": endpoint,
            "endpoint_stripped": endpoint_stripped,
            "have_key": bool(key),
            "key_length": len(key),
            "api_version": api_version,
            "model_id": model_id,
            "formrecognizer_analyze_url": form_url,
            "documentintelligence_analyze_url": docintel_url,
        },
    }

    return func.HttpResponse(
        json.dumps(payload),
        status_code=200,
        mimetype="application/json",
        headers=_cors_headers(origin),
    )
