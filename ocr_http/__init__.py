import json
import logging
import os
import time

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


def _json_response(payload, origin, status_code=200):
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        mimetype="application/json",
        headers=_cors_headers(origin),
    )


def _empty_fields():
    return {
        "amount_due": {"value": "", "confidence": 0.0},
        "due_date": {"value": "", "confidence": 0.0},
        "account_number": {"value": "", "confidence": 0.0},
        "sender": {"value": "", "confidence": 0.0},
        "service_address": {"value": "", "confidence": 0.0},
    }


def _http_post_bytes(url, headers, body, timeout=30.0):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def _http_get(url, headers, timeout=30.0):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _run_azure_ocr(body_bytes, target_lang):
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
            "debug": {"stub": False, "steps": debug_steps},
        }

    # Endpoint/key from env (handles multiple naming styles)
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

    # Stable v3.1 Form Recognizer
    api_version = os.getenv("AZURE_DI_API_VERSION", "2023-07-31")
    model_id = os.getenv("AZURE_DI_MODEL", "prebuilt-read")

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
            "Azure endpoint/key missing. "
            "Checked AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT / DOCUMENTINTELLIGENCE_ENDPOINT / "
            "AZURE_DI_ENDPOINT / AZURE_DOCINTEL_ENDPOINT and *_KEY/API_KEY."
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
            "debug": {"stub": False, "steps": debug_steps},
        }

    endpoint_stripped = endpoint.rstrip("/")
    analyze_url = (
        endpoint_stripped
        + "/formrecognizer/documentModels/"
        + model_id
        + ":analyze?api-version="
        + api_version
    )

    debug_steps.append("Stage 0: Received %d bytes." % length)
    debug_steps.append("Stage 1: Calling REST API %s" % analyze_url)

    headers = {
        "Content-Type": "application/octet-stream",
        "Ocp-Apim-Subscription-Key": key,
    }

    # 1) Analyze request
    try:
        resp = _http_post_bytes(analyze_url, headers, body_bytes, timeout=30.0)
        status_code = getattr(resp, "status", None) or resp.getcode()
        resp_text = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        debug_steps.append("REST call HTTPError %d: %s" % (e.code, body[:500]))
        return {
            "ok": False,
            "message": "Azure OCR analyze request failed.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "Azure OCR analyze request failed with HTTP %d." % e.code,
            "fields": _empty_fields(),
            "debug": {"stub": False, "steps": debug_steps},
        }
    except Exception as e:
        debug_steps.append("REST call to analyze endpoint failed: %s" % str(e))
        return {
            "ok": False,
            "message": "OCR failed while calling Azure OCR analyze endpoint.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "Server error while running OCR. Check Azure Function logs.",
            "fields": _empty_fields(),
            "debug": {"stub": False, "steps": debug_steps},
        }

    if status_code not in (200, 202):
        debug_steps.append_
