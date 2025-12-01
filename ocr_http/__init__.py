import json
import logging
import os
import io
from typing import Dict, Any

import azure.functions as func

# Allowed CORS origins
ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}

# Try to import Azure Document Intelligence, but don't crash if it's not installed.
try:
    from azure.core.credentials import AzureKeyCredential
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    AZURE_DI_SUPPORTED = True
    AZURE_DI_IMPORT_ERROR = ""
except Exception as e:
    AZURE_DI_SUPPORTED = False
    AZURE_DI_IMPORT_ERROR = str(e)


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


def _run_azure_ocr(body_bytes: bytes, target_lang: str) -> Dict[str, Any]:
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

    if not AZURE_DI_SUPPORTED:
        debug_steps.append("Azure Document Intelligence SDK import failed.")
        if AZURE_DI_IMPORT_ERROR:
            debug_steps.append("Import error: " + AZURE_DI_IMPORT_ERROR)
        return {
            "ok": False,
            "message": "OCR SDK not available on the server.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": (
                "The Azure Document Intelligence Python SDK is not installed or failed to import. "
                "Check requirements.txt for 'azure-ai-documentintelligence'."
            ),
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    # Support either your custom env var names or the official ones
    endpoint = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or os.getenv("DOCUMENTINTELLIGENCE_ENDPOINT")
        or ""
    )
    key = (
        os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        or os.getenv("DOCUMENTINTELLIGENCE_API_KEY")
        or ""
    )
    model_id = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID", "prebuilt-read")

    if not endpoint or not key:
        debug_steps.append(
            "Azure Document Intelligence endpoint/key missing. "
            "Set AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY "
            "or DOCUMENTINTELLIGENCE_ENDPOINT and DOCUMENTINTELLIGENCE_API_KEY."
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

    debug_steps.append("Stage 0: Received {} bytes.".format(length))
    debug_steps.append("Stage 1: Creating DocumentIntelligenceClient.")

    try:
        client = DocumentIntelligenceClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(key),
        )
    except Exception as e:
        logging.exception("Failed to initialize DocumentIntelligenceClient.")
        debug_steps.append("Failed to initialize DocumentIntelligenceClient: {}".format(str(e)))
        return {
            "ok": False,
            "message": "Failed to initialize Azure Document Intelligence client.",
            "received_bytes": length,
            "target_lang": target_lang,
            "ocr_text_snippet": "",
            "summary_translated": "",
            "summary_en": "Server error while initializing Azure OCR client.",
            "fields": _empty_fields(),
            "debug": {
                "stub": False,
                "steps": debug_steps,
            },
        }

    debug_steps.append("Stage 2: Calling Azure Document Intelligence model '{}'.".format(model_id))

    try:
        # Use BytesIO so it behaves like a file object
        stream = io.BytesIO(body_bytes)
        poller = client.begin_analyze_document(model_id, body=stream)
        result = poller.result()
        debug_steps.append("Stage 3: Azure returned result successfully.")
    except Exception as e:
        logging.exception("Azure OCR call failed.")
        debug_steps.append("Azure OCR call failed: {}".format(str(e)))
        return {
            "ok": False,
            "message": "OCR failed while calling Azure Document Intelligence.",
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

    # Collect text lines
    lines = []
    page_count = 0
    try:
        if getattr(result, "pages", None):
            page_count = len(result.pages)
            for page in result.pages:
                if getattr(page, "lines", None):
                    for line in page.lines:
                        # Defensive: some SDK shapes use .content
                        text = getattr(line, "content", "")
                        if text:
                            lines.append(text)
    except Exception as e:
        logging.exception("Failed to parse Azure OCR result.")
        debug_steps.append("Failed to parse Azure OCR result: {}".format(str(e)))

    debug_steps.append(
        "Stage 4: Extracted {} lines of text from {} page(s).".format(len(lines), page_count)
    )

    full_text = "\n".join(lines) if lines else ""
    snippet = full_text[:500] if full_text else ""

    return {
        "ok": True,
        "message": "OCR completed with Azure Document Intelligence.",
        "received_bytes": length,
        "target_lang": target_lang,
        "ocr_text_snippet": snippet,
        "summary_translated": "",
        "summary_en": (
            "OCR succeeded using Azure Document Intelligence. "
            "Summary/translation fields will be populated once LLM is wired in."
        ),
        "fields": _empty_fields(),
        "debug": {
            "stub": False,
            "azure": {
                "model_id": model_id,
                "page_count": page_count,
                "line_count": len(lines),
            },
            "steps": debug_steps,
        },
    }


def main(req: func.HttpRequest) -> func.HttpResponse:
    origin = req.headers.get("Origin")
    logging.info("mailbills/parse triggered, method=%s, origin=%s", req.method, origin)

    # CORS preflight
    if req.method.upper() == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    # Only POST is supported (but keep HTTP 200 so the frontend doesn't explode)
    if req.method.upper() != "POST":
        payload = {
            "ok": False,
            "message": "Use POST with PDF or image bytes.",
        }
        return _json_response(payload, origin, status_code=200)

    # Read body bytes
    try:
        body_bytes = req.get_body() or b""
    except Exception as e:
        logging.exception("Failed to read request body")
        payload = {
            "ok": False,
            "message": "Could not read request body.",
            "error": str(e),
            "fields": _empty_fields(),
        }
        return _json_response(payload, origin, status_code=200)

    length = len(body_bytes)
    target_lang = (req.params.get("target_lang") or "en").strip().lower() or "en"

    logging.info(
        "mailbills/parse received %d bytes, target_lang=%s",
        length,
        target_lang,
    )

    # Run OCR with full error handling
    payload = _run_azure_ocr(body_bytes, target_lang)
    return _json_response(payload, origin, status_code=200)
