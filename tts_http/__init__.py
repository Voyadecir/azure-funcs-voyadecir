import os
import json
import azure.functions as func
import urllib.request
import urllib.error

SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "")


def _ssml(text: str, lang: str, voice: str) -> str:
    if not voice:
        voice = "es-MX-DaliaNeural" if lang.lower().startswith("es") else "en-US-JennyNeural"
    lang_tag = "es-MX" if lang.lower().startswith("es") else "en-US"
    return f"""<speak version='1.0' xml:lang='{lang_tag}'>
  <voice name='{voice}'>{text}</voice>
</speak>"""


def main(req: func.HttpRequest) -> func.HttpResponse:
    # CORS preflight
    if req.method == "OPTIONS":
        return func.HttpResponse(status_code=204)

    # Basic guard on config
    if not SPEECH_KEY or not SPEECH_REGION:
        return func.HttpResponse(
            json.dumps({"error": "AZURE_SPEECH_KEY or AZURE_SPEECH_REGION not configured."}),
            status_code=500,
            mimetype="application/json",
        )

    try:
        data = req.get_json()
    except Exception:
        data = {}

    text = (data.get("text") or "").strip()
    lang = data.get("lang", "en-US")
    voice = data.get("voice", "")

    if not text:
        return func.HttpResponse(
            json.dumps({"error": "No text provided."}),
            status_code=400,
            mimetype="application/json",
        )

    tts_url = f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "voyadecir-tts",
    }
    ssml = _ssml(text, lang, voice)
    body_bytes = ssml.encode("utf-8")

    req_http = urllib.request.Request(tts_url, data=body_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req_http, timeout=30.0) as resp:
            audio = resp.read()
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        return func.HttpResponse(
            json.dumps({"error": "TTS failed", "detail": detail}),
            status_code=500,
            mimetype="application/json",
        )
    except urllib.error.URLError as e:
        return func.HttpResponse(
            json.dumps({"error": "TTS network error", "detail": str(e)}),
            status_code=500,
            mimetype="application/json",
        )

    if status >= 300:
        return func.HttpResponse(
            json.dumps({"error": "TTS failed", "status": status}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        body=audio,
        status_code=200,
        mimetype="audio/mpeg",
    )
