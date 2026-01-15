import json
import uuid
import azure.functions as func

def main(req: func.HttpRequest) -> func.HttpResponse:
    # If you see this response, deployment is correct and the function is running.
    return func.HttpResponse(
        json.dumps({
            "ok": True,
            "message": "mailbills_upload_url is running",
            "job_id": str(uuid.uuid4())
        }),
        status_code=200,
        mimetype="application/json",
    )
