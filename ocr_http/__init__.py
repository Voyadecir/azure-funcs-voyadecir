import json
import logging
import os
import base64
import time
from typing import Dict, Any

import azure.functions as func
import httpx

# Allowed CORS origins
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


def _empty_fields() -> Dict[str, Dict[str, Any]]:
    # Placeholder fields until we wire proper extraction
    return {
        "amount_due": {"value": "", "confidence": 0.0},
        "due_date": {"value": "", "confidence": 0.0},
        "account_number": {"value": "", "confidence": 0.0},
        "sender": {"value": "", "confidence": 0.0},
        "service_address": {"value": "", "confidence": 0.0},
    }


def _run_azure_ocr_httpx(body_bytes: bytes, target_lang: str) -> Dict[str, Any]:
    debug_steps = []
    length = len(body_bytes)

    if not body_bytes:
        debug_steps.append("No bytes in request body.")
        return {
            "ok": False,
            "message": "Empty request body. Send PDF or image bytes.",
            "received_bytes": 0,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "No file data was received by the server.",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    # Read config from multiple possible env var names

    # Endpoint (supports both DI-style and DOCINTEL-style names)
    endpoint = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
        or os.getenv("AZURE_DI_ENDPOINT")
        or os.getenv("AZURE_DOCINTEL_ENDPOINT")
        or ""
    )

    # Key (supports both DI-style and DOCINTEL-style names)
    key = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        or os.getenv("DOCUMENTINTELLIGENCE_API_KEY")
        or os.getenv("AZURE_DI_API_KEY")
        or os.getenv("AZURE_DOCINTEL_API_KEY")
        or os.getenv("AZURE_DOCINTEL_KEY")  # matches your current Function env var
        or ""
    )

    # Model + API version + polling params (with sane defaults)
    model_id = os.getenv("AZURE_DI_MODEL", "prebuilt-read")
    api_version = os.getenv("AZURE_DI_API_VERSION", "2024-02-29-preview")
    try:
        poll_attempts = int(os.getenv("AZURE_DI_POLL_ATTEMPTS", "10"))
    except ValueError:
        poll_attempts = 10
    try:
        poll_wait = float(os.getenv("AZURE_DI_MAX_POLL_WAIT", "1.0"))
    except ValueError:
        poll_wait = 1.0

    if not endpoint or not key:
        debug_steps.append(
            "Azure Document Intelligence endpoint/key missing. "
            "Checked AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT / DOCUMENTINTELLIGENCE_ENDPOINT / "
            "AZURE_DI_ENDPOINT / AZURE_DOCINTEL_ENDPOINT and corresponding *_KEY/API_KEY."
        )
        return {
            "ok": False,
            "message": "OCR is not configured on the server.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": (
                "Azure OCR is not configured. The server is running, but endpoint/key "
                "environment variables are missing or invalid."
            ),
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    endpoint = endpoint.rstrip("/")
    analyze_url = (
        f"{endpoint}/documentintelligence/documentModels/{model_id}:analyze"
        f"?_overload=analyzeDocument&api-version={api_version}"
    )

    debug_steps.append("Stage 0: Received {} bytes.".format(length))
    debug_steps.append("Stage 1: Calling REST API {}".format(analyze_url))

    # Encode file as base64 and send as JSON to the REST API
    base64_doc = base64.b64encode(body_bytes).decode("ascii")
    request_payload = {"base64Source": base64_doc}
    headers = {
        "Content-Type": "application/json",
        "Ocp-Apim-Subscription-Key": key,
    }

    try:
        resp = httpx.post(analyze_url, headers=headers, json=request_payload, timeout=30.0)
    except Exception as e:
        logging.exception("REST call to Azure Document Intelligence failed.")
        debug_steps.append("REST call to analyze endpoint failed: {}".format(str(e)))
        return {
            "ok": False,
            "message": "OCR failed while calling Azure Document Intelligence (analyze).",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "Server error while running OCR. Check Azure Function logs.",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    if resp.status_code not in (200, 202):
        debug_steps.append(
            "Analyze request returned HTTP {} with body: {}".format(
                resp.status_code, resp.text[:500]
            )
        )
        return {
            "ok": False,
            "message": "Azure OCR analyze request failed.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "Azure OCR analyze request failed with HTTP {}.".format(resp.status_code),
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    op_location = resp.headers.get("Operation-Location") or resp.headers.get("operation-location")
    if not op_location:
        debug_steps.append("Operation-Location header missing in analyze response.")
        return {
            "ok": False,
            "message": "Azure OCR did not return an operation location.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "Azure OCR did not return an operation location header.",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    debug_steps.append("Stage 2: Operation-Location = {}".format(op_location))

    # Poll for result
    status = "notStarted"
    result_json = None
    poll_headers = {"Ocp-Apim-Subscription-Key": key}

    for attempt in range(poll_attempts):
        try:
            time.sleep(poll_wait)
            poll_resp = httpx.get(op_location, headers=poll_headers, timeout=30.0)
        except Exception as e:
            logging.exception("Polling Azure OCR result failed.")
            debug_steps.append("Polling failed on attempt {}: {}".format(attempt + 1, str(e)))
            continue

        if poll_resp.status_code != 200:
            debug_steps.append(
                "Polling HTTP {} with body: {}".format(
                    poll_resp.status_code, poll_resp.text[:500]
                )
            )
            continue

        try:
            result_json = poll_resp.json()
        except Exception as e:
            debug_steps.append_
