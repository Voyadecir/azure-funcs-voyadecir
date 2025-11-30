import json
import logging
import azure.functions as func

# Allowed CORS origins
ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}


def _cors_headers(origin: str | None):
    origin_ok = origin if origin in ALLOWED_ORIGINS else ""
    return {
        "Access-Control-Allow-Origin": origin_ok,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _json_response(payload: dict, origin: str | None, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        mimetype="application/json",
        headers=_cors_headers(origin),
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    origin = req.headers.get("Origin")
    logging.info("mailbills/parse triggered, method=%s, origin=%s", req.method, origin)

    # CORS preflight
    if req.method.upper() == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    # Only POST is supported (but we keep HTTP 200 so the frontend doesn't die)
    if req.method.upper() != "POST":
        payload = {
            "ok": False,
            "message": "Use POST with PDF or image bytes.",
        }
        return _json_response(payload, origin, status_code=200)

    # Try to read body bytes
    try:
        body_bytes = req.get_body() or b""
    except Exception as e:
        logging.exception("Failed to read request body")
        payload = {
            "ok": False,
            "message": "Could not read request body.",
            "error": str(e),
        }
        return _json_response(payload, origin, status_code=200)

    length = len(body_bytes)
    target_lang = (req.params.get("target_lang") or "en").strip().lower() or "en"

    logging.info(
        "mailbills/parse received %d bytes, target_lang=%s",
        length,
        target_lang,
    )

    # DEBUG STUB PAYLOAD:
    # This pretends to be a full OCR+LLM response so the frontend has something to show.
    payload = {
        "ok": True,
        "message": "Debug stub: function alive, OCR/LLM not wired yet.",
        "received_bytes": length,
        "target_lang": target_lang,
        "ocr_text_snippet": f"[DEBUG] Received {length} bytes. OCR engine is not configured yet.",
        "summary_translated": "",
        "summary_en": (
            "Debug summary: the server received your file correctly, "
            "but OCR and LLM are not enabled in this stub."
        ),
        "fields": {
            "amount_due": {"value": "", "confidence": 0.0},
            "due_date": {"value": "", "confidence": 0.0},
            "account_number": {"value": "", "confidence": 0.0},
            "sender": {"value": "", "confidence": 0.0},
            "service_address": {"value": "", "confidence": 0.0},
        },
        "debug": {
            "stub": True,
        },
    }

    return _json_response(payload, origin, status_code=200)
