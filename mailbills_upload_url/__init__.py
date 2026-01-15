import json
import azure.functions as func

def main(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"ok": True, "message": "upload_url function is running"}),
        status_code=200,
        mimetype="application/json"
    )
