import os
import json
import azure.functions as func
import httpx

SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY", "")
SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "")

def _ssml(text: str, lang: str, voice: str) -> str:
    if not voice:
        voice = "es-MX-DaliaNeural" if lang.lower().startswith("es") else "en-US-JennyNeural"
    lang_tag = "es-MX" if lang.lower().startswith("es") else "en-US"
    return f"""<speak version='1.0' xml:lang='{lang_tag}'>
  <voice name='{voice}'>{text}</voice>
</speak>"""

async def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return func.HttpResponse(status_code=204)
    try:
        data = req.get_json()
    except Exception:
        data = {}
    text  = (data.get("text") or "").strip()
    lang  = data.get("lang", "en-US")
    voice = data.get("voice", "")
    if not text:
        return func.HttpResponse(json.dumps({"error":"No text provided."}), status_code=400, mimetype="application/json")
    tts_url = f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "voyadecir-tts"
    }
    ssml = _ssml(text, lang, voice)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(tts_url, headers=headers, content=ssml)
        if r.status_code >= 300:
            return func.HttpResponse(json.dumps({"error":"TTS failed","detail":r.text}), status_code=500, mimetype="application/json")
        return func.HttpResponse(body=r.content, status_code=200, mimetype="audio/mpeg")
