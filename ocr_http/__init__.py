import os
import json
import time
import base64
import mimetypes
import azure.functions as func
import httpx

VISION_ENDPOINT = os.getenv("AZURE_VISION_ENDPOINT", "").rstrip("/")
VISION_KEY      = os.getenv("AZURE_VISION_KEY", "")
DOCI_ENDPOINT   = os.getenv("AZURE_DOCINTEL_ENDPOINT", "").rstrip("/")
DOCI_KEY        = os.getenv("AZURE_DOCINTEL_KEY", "")
TRANSLATOR_PROXY = os.getenv("TRANSLATOR_PROXY_URL", "")

DOCI_ANALYZE_URL = f"{DOCI_ENDPOINT}/formrecognizer/documentModels/prebuilt-read:analyze?api-version=2023-07-31"

def _extract_filename_and_bytes(req: func.HttpRequest):
    ctype = req.headers.get('content-type', '')
    if ctype and ctype.startswith("multipart/form-data"):
        try:
            from multipart import MultipartParser
            body = req.get_body()
            boundary = ctype.split("boundary=")[-1].encode("utf-8")
            parser = MultipartParser(body, boundary)
            for part in parser.parts():
                if part.name in (b"file", b"upload", b"document"):
                    filename = (part.filename or b"upload.bin").decode("utf-8", "ignore")
                    return filename, part.value
            for part in parser.parts():
                return (part.filename or b"upload.bin").decode("utf-8", "ignore"), part.value
        except Exception:
            return "upload.bin", req.get_body()
    try:
        data = req.get_json()
        if isinstance(data, dict) and "file_b64" in data:
            return data.get("filename", "upload.bin"), base64.b64decode(data["file_b64"])
    except Exception:
        pass
    return "upload.bin", req.get_body()

def _guess_content_type(filename: str, default="application/octet-stream"):
    ctype, _ = mimetypes.guess_type(filename)
    if not ctype:
        if filename.lower().endswith(".pdf"):
            return "application/pdf"
        if filename.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")):
            return "image/png"
        return default
    return ctype

def _summarize_plain_en(raw_text: str) -> str:
    text = raw_text[:6000]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    joined = " ".join(lines)
    summary_bits = []
    import re
    amount = re.search(r"(amount due|pay\s*by|total due|balance due)[^\d$]*([\$€£]?\s?\d[\d,\.]*)", joined, re.I)
    if amount:
        summary_bits.append(f"Amount due: {amount.group(2)}")
    due = re.search(r"(due date|pay by|fecha de vencimiento)[:\s]*([A-Za-z]{3,9}\s\d{1,2},\s?\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", joined, re.I)
    if due:
        summary_bits.append(f"Due date: {due.group(2)}")
    acct = re.search(r"(account(?:\s*#|\s*number)?|acct(?:\s*#)?)[:\s]*([A-Z0-9\-]{5,})", joined, re.I)
    if acct:
        summary_bits.append(f"Account: {acct.group(2)}")
    if lines:
        sender = lines[0][:80]
        if sender:
            summary_bits.append(f"Sender: {sender}")
    if not summary_bits:
        summary_bits.append("No obvious totals or dates detected.")
    return " | ".join(summary_bits)

async def _translate_via_proxy(text: str, target_lang: str) -> str:
    if not TRANSLATOR_PROXY:
        return f"[{target_lang}] {text}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(TRANSLATOR_PROXY, json={"text": text, "target_lang": target_lang})
            r.raise_for_status()
            data = r.json()
            return data.get("translated_text") or data.get("translation") or f"[{target_lang}] {text}"
        except Exception:
            return f"[{target_lang}] {text}"

async def _ocr_with_document_intel(file_bytes: bytes, content_type: str):
    headers = {"Ocp-Apim-Subscription-Key": DOCI_KEY, "Content-Type": content_type}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(DOCI_ANALYZE_URL, headers=headers, content=file_bytes)
        if r.status_code >= 300:
            return None, f"Analyze failed: {r.status_code} {r.text}"
        op_url = r.headers.get("operation-location") or r.headers.get("Operation-Location")
        if not op_url:
            return None, "Missing operation-location header"
        for _ in range(30):
            rr = await client.get(op_url, headers={"Ocp-Apim-Subscription-Key": DOCI_KEY})
            if rr.status_code >= 300:
                return None, f"Status poll failed: {rr.status_code} {rr.text}"
            data = rr.json()
            status = (data.get("status") or "").lower()
            if status in ("succeeded", "failed"):
                if status == "failed":
                    return None, json.dumps(data)
                result = data.get("analyzeResult") or {}
                content = result.get("content") or ""
                return content, None
            time.sleep(1.0)
        return None, "Timed out waiting for analysis"

async def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse(status_code=204)
    target_lang = req.params.get("target_lang") or "es"
    filename, file_bytes = _extract_filename_and_bytes(req)
    if not file_bytes:
        return func.HttpResponse(json.dumps({"error": "No file received."}), status_code=400, mimetype="application/json")
    ctype = _guess_content_type(filename)
    ocr_text, err = await _ocr_with_document_intel(file_bytes, ctype)
    if err:
        return func.HttpResponse(json.dumps({"error": "OCR failed", "detail": err}), status_code=500, mimetype="application/json")
    summary_en = _summarize_plain_en(ocr_text)
    summary_trans = await _translate_via_proxy(summary_en, target_lang)
    payload = {
        "filename": filename,
        "content_type": ctype,
        "summary_en": summary_en,
        "summary_translated": summary_trans,
        "target_lang": target_lang,
        "raw_text": ocr_text[:20000]
    }
    return func.HttpResponse(json.dumps(payload), status_code=200, mimetype="application/json")
