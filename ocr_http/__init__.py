import json
import logging
import os
import io

import azure.functions as func
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient

# Allowed CORS origins
ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}

# Azure Document Intelligence config
AZURE_DI_ENDPOINT = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
AZURE_DI_KEY = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
AZURE_DI_MODEL_ID = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_MODEL_ID", "prebuilt-read")

document_intelligence_client: DocumentIntelligenceClient | None = None
if AZURE_DI_ENDPOINT and AZURE_DI_KEY:
    try:
        document_intelligence_client = DocumentIntelligenceClient(
            endpoint=AZURE_DI_ENDPOINT,
            credential=AzureKeyCredential(AZURE_DI_KEY),
        )
        logging.info("Initialized Azure Document Intelligence client.")
    except Exception:
        logging.exception("Failed to initialize Document Intelligence client.")


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


def _empty_fields():
    # Placeholder; we’ll fill this in later when we do real field extraction
    return {
        "amount_due": {"value": "", "confidence": 0.0},
        "due_date": {"value": "", "confidence": 0.0},
        "account_number": {"value": "", "confidence": 0.0},
        "sender": {"value": "", "confidence": 0.0},
        "service_address": {"value": "", "confidence": 0.0},
    }


def _run_azure_ocr(body_bytes: bytes, target_lang: str) -> dict:
    debug_steps: list[str] = []
    length = len(body_bytes)

    if not body_bytes:
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
                "steps": ["No bytes in request body."],
            },
        }

    if document_intelligence_client is None:
        debug_steps.append(
            "Azure Document Intelligence client is not initialized. "
            "Check AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT / AZURE_DOCUMENT_INTELLIGENCE_KEY env vars."
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

    debug_steps.append(f"Stage 0: Received {length} bytes.")
    debug_steps.append(f"Stage 1: Calling Azure Document Intelligence model '{AZURE_DI_MODEL_ID}'.")

    # Call Azure AI Document Intelligence (prebuilt-read or whatever model you configure)
    poller = document_intelligence_client.begin_analyze_document(
        AZURE_DI_MODEL_ID,
        body=io.BytesIO(body_bytes),
    )
    result = poller.result()
    debug_steps.append("Stage 2: Azure returned result successfully.")

    # Collect text as simple lines
    lines: list[str] = []
    page_count = 0
    if result.pages:
        page_count = len(result.pages)
        for page in result.pages:
            if page.lines:
                for line in page.lines:
                    lines.append(line.content)

    full_text = "\n".join(lines)
    debug_steps.append(
        f"Stage 3: Extracted {len(lines)} lines of text from {page_count} page(s)."
    )

    # For now we don’t do LLM summary; just say OCR succeeded,
    # front-end still gets exactly the keys it expects.
    snippet = full_text[:500] if full_text else ""

    payload = {
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
                "model_id": AZURE_DI_MODEL_ID,
                "page_count": page_count,
                "line_count": len(lines),
            },
            "steps": debug_steps,
        },
    }
    return payload


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

    # Try to read body bytes
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

    try:
        payload = _run_azure_ocr(body_bytes, target_lang)
    except Exception as e:
        logging.exception("Azure OCR call failed.")
        payload = {
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
                "exception": str(e),
            },
        }

    return _json_response(payload, origin, status_code=200)
