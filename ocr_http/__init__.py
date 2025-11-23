import json
import azure.functions as func

ALLOWED_ORIGINS = {"https://voyadecir.com", "https://www.voyadecir.com"}

def _cors_headers(origin: str | None):
    origin_ok = origin if origin in ALLOWED_ORIGINS else ""
    return {
        "Access-Control-Allow-Origin": origin_ok,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    }

def main(req: func.HttpRequest) -> func.HttpResponse:
    origin = req.headers.get("Origin")
    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=204, headers=_cors_headers(origin))

    # Simple, known-good response so we can verify the route exists
    body = {"ok": True, "message": "mailbills/parse alive"}
    return func.HttpResponse(
        json.dumps(body),
        status_code=200,
        mimetype="application/json",
        headers=_cors_headers(origin)
    )
