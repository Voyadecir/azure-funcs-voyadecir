import json
import logging
import azure.functions as func

ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}

def _cors_headers(origin: str | None):
    origin_ok = origin if origin in ALLOWED_ORIGINS else ""
    return {
        "Access-Control-Allow-Origin": origin_ok,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    }

def main(req: func.HttpRequest) -> func.HttpResponse:
    origin = req.headers.get("Origin")

    # CORS preflight
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    try:
        # Consume body to avoid stream weirdness
        body_bytes = req.get_body() or b""
        # Minimal smoke-test response
        payload = {
            "ok": True,
            "message": "mailbills/parse alive",
            "received_bytes": len(body_bytes)
        }
        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json",
            headers=_cors_headers(origin),
        )
    except Exception as e:
        logging.exception("mailbills/parse crashed")
        # Return details to help you debug without a 500
        payload = {"ok": False, "error": str(e)}
        return func.HttpResponse(
            json.dumps(payload),
            status_code=200,
            mimetype="application/json",
            headers=_cors_headers(origin),
        )
