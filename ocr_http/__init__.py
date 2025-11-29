import json
import logging
import os
from typing import Optional

import azure.functions as func
import requests

# Allowed browser origins (CORS)
ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}

# === ENV VARS ===
# Azure Document Intelligence (Read OCR)
DOCINTEL_ENDPOINT = os.getenv("AZURE_DOCINTEL_ENDPOINT", "").rstrip("/")
DOCINTEL_KEY = os.getenv("AZURE_DOCINTEL_KEY", "")

# Azure OpenAI
OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY", "")
OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")  # e.g. "gpt-4o-mini"
OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")


def _cors_headers(origin: Optional[str]):
    origin_ok = origin if origin in ALLOWED_ORIGINS else ""
    return {
        "Access-Control-Allow-Origin": origin_ok,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _json_response(payload: dict, status_code: int, origin: Optional[str]) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        mimetype="application/json",
        headers=_cors_headers(origin),
    )


def _error(status_code: int, message: str, origin: Optional[str]) -> func.HttpResponse:
    logging.error("mailbills/parse error %s: %s", status_code, message)
    return _json_response({"ok": False, "error": message}, status_code, origin)


def _run_ocr_with_docintelligence(file_bytes: bytes, content_type: str):
    """
    Call Azure Document Intelligence Read model and return (full_text, debug_info).
    """

    if not DOCINTEL_ENDPOINT or not DOCINTEL_KEY:
        logging.warning("DocIntelligence env vars missing, skipping OCR.")
        return "", {
            "used": False,
            "reason": "missing AZURE_DOCINTEL_ENDPOINT or AZURE_DOCINTEL_KEY",
        }

    url = f"{DOCINTEL_ENDPOINT}/documentintelligence/documentModels/prebuilt-read:analyze"
    params = {"api-version": "2024-02-29-preview"}  # adjust if your resource uses a different version

    headers = {
        "Ocp-Apim-Subscription-Key": DOCINTEL_KEY,
        "Content-Type": content_type or "application/octet-stream",
    }

    logging.info("Calling Document Intelligence Read at %s", url)
    resp = requests.post(url, params=params, headers=headers, data=file_bytes, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    # Newer Document Intelligence schema: full text under "content"
    full_text = result.get("content") or ""
    snippet = full_text[:2000]

    debug = {
        "used": True,
        "chars": len(full_text),
        "snippet_preview": snippet,
    }
    return full_text, debug


def _run_llm_extract_and_summarise(full_text: str, target_lang: str):
    """
    Call Azure OpenAI to summarise and extract key billing fields.
    Returns (summary, fields_dict).
    """

    if not full_text:
        return "", {}

    if not OPENAI_ENDPOINT or not OPENAI_KEY or not OPENAI_DEPLOYMENT:
        logging.warning("OpenAI env vars missing, skipping LLM extraction.")
        return "", {}

    url = f"{OPENAI_ENDPOINT}/openai/deployments/{OPENAI_DEPLOYMENT}/chat/completions"
    params = {"api-version": OPENAI_API_VERSION}

    system_prompt = (
        "You are an assistant that reads utility bills and postal mail. "
        "You extract key billing fields and write a concise summary. "
        "ALWAYS respond with valid JSON only, no extra text. "
        "Use this exact schema:\n"
        "{\n"
        '  \"summary\": \"<short summary in target language>\",\n'
        '  \"fields\": {\n'
        '    \"amount_due\": {\"value\": \"<number or string>\", \"confidence\": 0-1},\n'
        '    \"due_date\": {\"value\": \"<date or phrase>\", \"confidence\": 0-1},\n'
        '    \"account_number\": {\"value\": \"<account id>\", \"confidence\": 0-1},\n'
        '    \"sender\": {\"value\": \"<utility or sender name>\", \"confidence\": 0-1},\n'
        '    \"service_address\": {\"value\": \"<service or mailing address>\", \"confidence\": 0-1}\n'
        "  }\n"
        "}\n"
        "If a field is unknown, use value: \"\" and confidence: 0."
    )

    user_prompt = (
        f"Target language for the summary: {target_lang}\n\n"
        "Document text (may be long, focus on billing / key info):\n"
        f"{full_text[:8000]}"
    )

    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Content-Type": "application/json",
        "api-key": OPENAI_KEY,
    }

    logging.info("Calling Azure OpenAI for summary + fields.")
    resp = requests.post(url, params=params, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "{}")
    )

    try:
        parsed = json.loads(content)
    except Exception:
        logging.exception("Failed to parse LLM JSON, content was: %s", content)
        return "", {}

    summary = parsed.get("summary") or ""
    fields = parsed.get("fields") or {}
    return summary, fields


def main(req: func.HttpRequest) -> func.HttpResponse:
    origin = req.headers.get("Origin")
    logging.info("mailbills/parse triggered, method=%s, origin=%s", req.method, origin)

    # CORS preflight
    if req.method.upper() == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    if req.method.upper() != "POST":
        return _error(405, "Method not allowed. Use POST.", origin)

    try:
        # target_lang from query, default "en"
        target_lang = (req.params.get("target_lang") or "en").lower().strip() or "en"
    except Exception:
        target_lang = "en"

    try:
        body_bytes = req.get_body() or b""
    except Exception as e:
        return _error(400, f"Could not read request body: {e}", origin)

    if not body_bytes:
        return _error(400, "Empty request body. Send PDF or image bytes.", origin)

    content_type = req.headers.get("Content-Type", "application/octet-stream")
    logging.info(
        "mailbills/parse received %d bytes, content-type=%s, target_lang=%s",
        len(body_bytes),
        content_type,
        target_lang,
    )

    try:
        # 1) OCR
        full_text, ocr_debug = _run_ocr_with_docintelligence(body_bytes, content_type)

        # 2) LLM summary + field extraction
        summary, fields = _run_llm_extract_and_summarise(full_text, target_lang)

        payload = {
            "ok": True,
            "message": "mailbills/parse success",
            "target_lang": target_lang,
            "ocr_text_snippet": (full_text or "")[:4000],
            "summary_translated": summary,
            "summary_en": summary if target_lang == "en" else "",
            "fields": fields or {},
            "debug": {
                "ocr": ocr_debug,
                "ocr_chars": len(full_text or ""),
                "has_summary": bool(summary),
            },
        }
        return _json_response(payload, 200, origin)

    except requests.HTTPError as http_err:
        logging.exception("HTTP error from upstream service.")
        return _error(502, f"Upstream HTTP error: {str(http_err)}", origin)
    except Exception as e:
        logging.exception("Unhandled exception in mailbills/parse.")
        return _error(500, f"Internal error: {str(e)}", origin)
