import os
import json
import logging
import azure.functions as func
import urllib.request
import urllib.error

SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY", "").strip()
SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "").strip()


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "https://voyadecir.com",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _ssml(text: str, lang: str, voice: str) -> str:
    if not voice:
        # Default voice by language
        voice = "es-MX-DaliaNeural" if lang.lower().startswith("es") else "en-US-JennyNeural"
    lang_tag = "es-MX" if lang.lower().startswith("es") else "en-US"
    return f"""<speak version='1.0' xml:lang='{lang_tag}'>
  <voice name='{voice}'>{text}</voice>
</speak>"""


def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("tts_http: request received, method=%s", req.method)

    # CORS preflight
    if req.method == "OPTIONS":
        return func.HttpResponse(status_code=204, headers=_cors_headers())

    # Check config
    if not SPEECH_KEY or not SPEECH_REGION:
        logging.error("tts_http: missing AZURE_SPEECH_KEY or AZURE_SPEECH_REGION")
        body = json.dumps({"error": "Server TTS config missing."})
        return func.HttpResponse(
            body,
            status_code=500,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    # Parse body
    try:
        data = req.get_json()
    except Exception:
        data = {}

    text  = (data.get("text") or "").strip()
    lang  = (data.get("lang") or "en-US").strip()
    voice = (data.get("voice") or "").strip()

    if not text:
        body = json.dumps({"error": "No text provided."})
        return func.HttpResponse(
            body,
            status_code=400,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    # Build TTS request
    tts_url = f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    ssml = _ssml(text, lang, voice)
    logging.info(
        "tts_http: calling TTS region=%s, lang=%s, voice=%s",
        SPEECH_REGION,
        lang,
        voice or "(auto)",
    )

    req_headers = {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "voyadecir-tts",
    }

    http_req = urllib.request.Request(
        tts_url,
        data=ssml.encode("utf-8"),
        headers=req_headers,
        method="POST",
    )

    # Call Azure TTS
    try:
        with urllib.request.urlopen(http_req, timeout=30) as resp:
            audio_bytes = resp.read()
            logging.info("tts_http: TTS success, bytes=%d", len(audio_bytes))
            return func.HttpResponse(
                body=audio_bytes,
                status_code=200,
                mimetype="audio/mpeg",
                headers=_cors_headers(),
            )

    except urllib.error.HTTPError as e:
        # Read error body for debugging
        try:
            err_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            err_body = ""
        logging.error("tts_http: HTTPError from TTS: %s %s", e.code, err_body)
        body = json.dumps({"error": "TTS failed", "status": e.code})
        return func.HttpResponse(
            body,
            status_code=500,
            mimetype="application/json",
            headers=_cors_headers(),
        )

    except urllib.error.URLError as e:
        logging.error("tts_http: URLError from TTS: %s", str(e))
        body = json.dumps({"error": "TTS network error"})
        return func.HttpResponse(
            body,
            status_code=500,
            mimetype="application/json",
            headers=_cors_headers(),
        )
