import json
import logging
import os
import time
from typing import Dict, Any

import azure.functions as func
import urllib.request
import urllib.error

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


def _http_post_bytes(url: str, headers: Dict[str, str], body: bytes, timeout: float = 30.0):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def _http_get(url: str, headers: Dict[str, str], timeout: float = 30.0):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _run_azure_ocr(body_bytes: bytes, target_lang: str) -> Dict[str, Any]:
    """
    End-to-end:
      1) POST raw file bytes to Form Recognizer v3.1 analyze endpoint
      2) Poll Operation-Location until status == 'succeeded'
      3) Extract text lines into a single string + snippet
    """
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

    # Endpoint (supports multiple env var names)
    endpoint = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
        or os.getenv("AZURE_DI_ENDPOINT")
        or os.getenv("AZURE_DOCINTEL_ENDPOINT")
        or ""
    )

    # Key (supports multiple env var names)
    key = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        or os.getenv("DOCUMENTINTELLIGENCE_API_KEY")
        or os.getenv("AZURE_DI_API_KEY")
        or os.getenv("AZURE_DOCINTEL_API_KEY")
        or os.getenv("AZURE_DOCINTEL_KEY")
        or ""
    )

    # Use stable v3.1 Form Recognizer API by default
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

    endpoint_stripped = endpoint.rstrip("/")
    analyze_url = (
        f"{endpoint_stripped}/formrecognizer/documentModels/{model_id}:analyze"
        f"?api-version={api_version}"
    )

    debug_steps.append(f"Stage 0: Received {length} bytes.")
    debug_steps.append(f"Stage 1: Calling REST API {analyze_url}")

    # 1) Send analyze request with raw bytes
    headers = {
        # Raw bytes; service will detect content type, PDF is fine here
        "Content-Type": "application/octet-stream",
        "Ocp-Apim-Subscription-Key": key,
    }

    try:
        resp = _http_post_bytes(analyze_url, headers, body_bytes, timeout=30.0)
        status_code = getattr(resp, "status", None) or resp.getcode()
        resp_text = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        logging.exception("REST call to Azure Form Recognizer failed with HTTPError.")
        debug_steps.append(f"REST call HTTPError {e.code}: {body[:500]}")
        return {
            "ok": False,
            "message": "Azure OCR analyze request failed.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": f"Azure OCR analyze request failed with HTTP {e.code}.",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }
    except Exception as e:
        logging.exception("REST call to Azure Form Recognizer failed.")
        debug_steps.append(f"REST call to analyze endpoint failed: {str(e)}")
        return {
            "ok": False,
            "message": "OCR failed while calling Azure OCR analyze endpoint.",
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

    if status_code not in (200, 202):
        debug_steps.append(
            f"Analyze request returned HTTP {status_code} with body: {resp_text[:500]}"
        )
        return {
            "ok": False,
            "message": "Azure OCR analyze request failed.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": f"Azure OCR analyze request failed with HTTP {status_code}.",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    op_location = None
    if hasattr(resp, "getheader"):
        op_location = resp.getheader("Operation-Location") or resp.getheader("operation-location")

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

    debug_steps.append(f"Stage 2: Operation-Location = {op_location}")

    # 2) Poll for result
    status = "notStarted"
    result_json: Dict[str, Any] | None = None
    poll_headers = {"Ocp-Apim-Subscription-Key": key}

    for attempt in range(poll_attempts):
        try:
            time.sleep(poll_wait)
            poll_resp = _http_get(op_location, poll_headers, timeout=30.0)
            poll_status = getattr(poll_resp, "status", None) or poll_resp.getcode()
            poll_text = poll_resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            logging.exception("Polling Azure OCR result failed with HTTPError.")
            debug_steps.append(
                f"Polling HTTPError {e.code} on attempt {attempt + 1}: {body[:500]}"
            )
            continue
        except Exception as e:
            logging.exception("Polling Azure OCR result failed.")
            debug_steps.append(f"Polling failed on attempt {attempt + 1}: {str(e)}")
            continue

        if poll_status != 200:
            debug_steps.append(
                f"Polling HTTP {poll_status} with body: {poll_text[:500]}"
            )
            continue

        try:
            result_json = json.loads(poll_text)
        except Exception as e:
            debug_steps.append(f"Failed to parse polling JSON: {str(e)}")
            continue

        status = result_json.get("status", "")
        debug_steps.append(f"Stage 3 (attempt {attempt + 1}): status = {status}")

        if status in ("succeeded", "failed", "partiallySucceeded"):
            break

    if not result_json:
        return {
            "ok": False,
            "message": "Did not receive a valid JSON result from Azure OCR.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "Azure OCR did not return a valid JSON result.",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    if status != "succeeded":
        debug_steps.append(f"Final status from Azure OCR: {status}")
        return {
            "ok": False,
            "message": f"Azure OCR did not succeed (status={status}).",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": f"Azure OCR did not complete successfully (status={status}).",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
                "raw_status": status,
            },
        }

    # 3) Extract lines of text
    lines: list[str] = []
    page_count = 0
    try:
        analyze_result = result_json.get("analyzeResult", {}) or {}
        pages = analyze_result.get("pages", []) or []
        page_count = len(pages)
        for page in pages:
            page_lines = page.get("lines", []) or []
            for line in page_lines:
                text = line.get("content") or ""
                if text:
                    lines.append(text)
    except Exception as e:
        logging.exception("Failed to parse Azure OCR analyzeResult.")
        debug_steps.append(f"Failed to parse analyzeResult: {str(e)}")

    debug_steps.append(
        f"Stage 4: Extracted {len(lines)} lines of text from {page_count} page(s)."
    )

    full_text = "\n".join(lines) if lines else ""
    snippet = full_text[:500] if full_text else ""

    return {
        "ok": True,
        "message": "OCR completed using Azure Form Recognizer v3.1.",
        "received_bytes": length,
        "target_lang": target_lang,
        "ocr_text_snippet": snippet,
        "summary_translated": "",
        "summary_en": (
            "OCR succeeded using A
