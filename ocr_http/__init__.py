import logging
import os
import json
import time
from typing import List, Dict, Any, Tuple, Optional

import azure.functions as func
import requests

logger = logging.getLogger("ocr_http")


def _json_response(body: Dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    """Small helper to return JSON HTTP responses."""
    return func.HttpResponse(
        body=json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
    )


def _get_config() -> Dict[str, Any]:
    """Read Azure Document Intelligence settings from environment."""
    endpoint = (
        os.environ.get("DOCINTEL_ENDPOINT")
        or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or ""
    )
    key = (
        os.environ.get("DOCINTEL_KEY")
        or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        or ""
    )
    api_version = os.environ.get("DOCINTEL_API_VERSION") or "2023-07-31"
    model_id = os.environ.get("DOCINTEL_MODEL_ID") or "prebuilt-read"

    return {
        "endpoint": endpoint.rstrip("/"),
        "key": key,
        "api_version": api_version,
        "model_id": model_id,
    }


def _analyze_document(
    data: bytes,
    content_type: str,
    debug_steps: List[str],
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Call Azure Document Intelligence /formrecognizer/documentModels/{model_id}:analyze
    and return the operation-location URL if successful.
    """
    cfg = _get_config()
    endpoint = cfg["endpoint"]
    key = cfg["key"]
    api_version = cfg["api_version"]
    model_id = cfg["model_id"]

    if not endpoint or not key:
        debug_steps.append("Missing endpoint or key in environment.")
        return None, {
            "ok": False,
            "message": "Azure Document Intelligence endpoint or key is not configured.",
        }

    analyze_url = f"{endpoint}/formrecognizer/documentModels/{model_id}:analyze"
    params = {"api-version": api_version}

    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": content_type or "application/octet-stream",
    }

    debug_steps.append(f"Calling analyze: {analyze_url}?api-version={api_version}")

    resp = requests.post(analyze_url, params=params, headers=headers, data=data)
    if resp.status_code != 202:
        debug_steps.append(f"Analyze HTTP {resp.status_code}: {resp.text[:500]}")
        return None, {
            "ok": False,
            "message": f"Analyze call failed with HTTP {resp.status_code}.",
            "body_preview": resp.text[:500],
        }

    op_location = resp.headers.get("operation-location") or resp.headers.get(
        "Operation-Location"
    )
    debug_steps.append(f"operation-location: {op_location}")
    if not op_location:
        return None, {
            "ok": False,
            "message": "Analyze call did not return operation-location header.",
        }

    return op_location, None


def _poll_result(
    operation_url: str,
    debug_steps: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Poll the operation-location URL until status == succeeded or failed, or we time out.
    """
    cfg = _get_config()
    key = cfg["key"]

    headers = {
        "Ocp-Apim-Subscription-Key": key,
    }

    for attempt in range(15):
        debug_steps.append(f"Polling result attempt {attempt + 1}")
        resp = requests.get(operation_url, headers=headers)

        if resp.status_code != 200:
            debug_steps.append(f"Poll HTTP {resp.status_code}: {resp.text[:500]}")
            return None, {
                "ok": False,
                "message": f"Poll failed with HTTP {resp.status_code}.",
                "body_preview": resp.text[:500],
            }

        data = resp.json()
        status = data.get("status") or data.get("analyzeResult", {}).get("status")
        debug_steps.append(f"status={status}")

        if status in ("succeeded", "Succeeded"):
            return data, None
        if status in ("failed", "Failed"):
            return None, {
                "ok": False,
                "message": "Analyze operation reported failed.",
                "raw": data,
            }

        time.sleep(1.0)

    return None, {
        "ok": False,
        "message": "Timed out waiting for analyze result.",
    }


def _extract_text(result: Dict[str, Any]) -> Tuple[str, str]:
    """
    Extract full text and a short snippet from the Azure result.
    Supports both { content } and { analyzeResult: { content } } shapes.
    """
    full_text = ""
    if "content" in result:
        full_text = result.get("content") or ""
    elif "analyzeResult" in result and isinstance(result["analyzeResult"], dict):
        full_text = result["analyzeResult"].get("content") or ""

    snippet = full_text[:1000]
    return snippet, full_text


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger entry point for OCR:
    - Accepts binary body (PDF/image)
    - Calls Azure Document Intelligence Read
    - Polls until result is ready
    - Returns OCR text + snippet + stub fields
    """
    debug_steps: List[str] = []

    try:
        body = req.get_body()
        size = len(body or b"")
        debug_steps.append(f"Received {size} bytes.")

        if not body:
            return _json_response(
                {
                    "ok": False,
                    "message": "Request body is empty.",
                    "debug": {"steps": debug_steps},
                },
                status_code=400,
            )

        content_type = req.headers.get("Content-Type", "application/octet-stream")

        # 1) Start analyze
        op_url, err = _analyze_document(body, content_type, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            return _json_response(err, status_code=500)

        # 2) Poll for result
        result, err = _poll_result(op_url, debug_steps)
        if err is not None:
            err["debug"] = {"steps": debug_steps}
            return _json_response(err, status_code=500)

        # 3) Extract text
        snippet, full_text = _extract_text(result)

        response_body = {
            "ok": True,
            "message": "OCR succeeded.",
            "ocr_text_snippet": snippet,
            "ocr_text": full_text,
            "summary_en": "",
            "summary_translated": "",
            "fields": {
                "amount_due": {"value": "", "confidence": 0.0},
                "due_date": {"value": "", "confidence": 0.0},
                "account_number": {"value": "", "confidence": 0.0},
                "sender": {"value": "", "confidence": 0.0},
                "service_address": {"value": "", "confidence": 0.0},
            },
            "debug": {
                "steps": debug_steps,
                "operation_url": op_url,
            },
        }

        return _json_response(response_body, status_code=200)

    except Exception as exc:
        logger.exception("Unhandled exception in ocr_http", exc_info=exc)
        return _json_response(
            {
                "ok": False,
                "message": "Unhandled exception in ocr_http.",
                "error": str(exc),
                "debug": {"steps": debug_steps},
            },
            status_code=500,
        )
