import json
import logging

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
    logging.info("DEBUG mailbills/parse minimal handler, method=%s, origin=%s", req.method, origin)

    # CORS preflight
    if req.method.upper() == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    # Only POST is supported (keep 200 so frontend doesn't explode)
    if req.method.upper() != "POST":
        payload = {
            "ok": False,
            "message": "Use POST with PDF or image bytes. (DEBUG MINIMAL)",
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
        logging.exception("DEBUG: Failed to read request body")
        payload = {
            "ok": False,
            "message": "DEBUG: Could not read request body.",
            "error": str(e),
        }
        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json",
            headers=_cors_headers(origin),
        )

    payload = {
        "ok": True,
        "message": "DEBUG minimal function alive.",
        "received_bytes": length,
    }
    return func.HttpResponse(
        json.dumps(payload),
        status_code=200,
        mimetype="application/json",
        headers=_cors_headers(origin),
    )
