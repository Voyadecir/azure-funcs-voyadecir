import logging
import os
import json
import time
from typing import List, Dict, Any, Tuple, Optional

import azure.functions as func
import urllib.request
import urllib.error
import urllib.parse

logger = logging.getLogger("ocr_http")


def _json_response(body: Dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    """Small helper to return JSON HTTP responses."""
    return func.HttpResponse(
        body=json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
    )


def _get_config() -> Dict[str, Any]:
    """
    Read Azure Document Intelligence settings from environment.

    Supports all of these patterns:

    - DOCINTEL_*                          (legacy / generic)
    - AZURE_DOCUMENT_INTELLIGENCE_*       (older naming)
    - AZURE_DOCINTEL_*                    (your Function App settings)
    - AZURE_DI_*                          (your Render settings)
    """
    endpoint = (
        os.environ.get("DOCINTEL_ENDPOINT")
        or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        or os.environ.get("AZURE_DOCINTEL_ENDPOINT")
        or os.environ.get("AZURE_DI_ENDPOINT")
        or ""
    )
    key = (
        os.environ.get("DOCINTEL_KEY")
        or os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        or os.environ.get("AZURE_DOCINTEL_KEY")
        or os.environ.get("AZURE_DI_API_KEY")
        or ""
    )
    api_version = (
        os.environ.get("DOCINTEL_API_VERSION")
        or os.environ.get("AZURE_DI_API_VERSION")
        or os.environ.get("AZURE_DOCINTEL_API_VERSION")
        or "2024-02-29-preview"
    )
    model_id = (
        os.environ.get("DOCINTEL_MODEL_ID")
        or os.environ.get("AZURE_DI_MODEL")
        or os.environ.get("AZURE_DOCINTEL_MODEL_ID")
        or "prebuilt-read"
    )

    return {
        "endpoint": endpoint.rstrip("/"),
        "key": key,
        "api_version": api_version,
        "model_id": model_id,
    }


def _http_post(
    url: str,
    params: Dict[str, str],
    headers: Dict[str, str],
    data: bytes,
    timeout: float = 30.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    """
    Simple POST using urllib (no third-party deps).
    Returns (status_code, body_bytes, headers_dict).
    """
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read()
            resp_headers = dict(resp.getheaders())
            return status, body, resp_headers
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body, dict(e.headers or {})
    except urllib.error.URLError as e:
        raise RuntimeError(f"HTTP POST failed: {e}") from e


def _http_get(
    url: str,
    headers: Dict[str, str],
    timeout: float = 30.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    """
    Simple GET using urllib (no third-party deps).
    Returns (status_code, body_bytes, headers_dict).
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
