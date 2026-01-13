import azure.functions as func
import uuid
import json
import os
from azure.storage.blob import BlobServiceClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

def main(req: func.HttpRequest) -> func.HttpResponse:
    # Generate a job ID immediately
    job_id = str(uuid.uuid4())

    # Read raw PDF bytes from request
    pdf_bytes = req.get_body()
    if not pdf_bytes:
        return func.HttpResponse(
            "No file uploaded",
            status_code=400
        )

    # Blob storage setup (already authenticated via AzureWebJobsStorage)
    blob_service = BlobServiceClient.from_connection_string(
        os.environ["AzureWebJobsStorage"]
    )
    container = blob_service.get_container_client("mailbills")

    # Save uploaded PDF (optional but useful for debugging)
    container.upload_blob(
        name=f"uploads/{job_id}.pdf",
        data=pdf_bytes,
        overwrite=True
    )

    # Start Document Intelligence OCR (ASYNC)
    client = DocumentAnalysisClient(
        endpoint=os.environ["AZURE_DOCINTEL_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["AZURE_DOCINTEL_KEY"])
    )

    poller = client.begin_analyze_document(
        "prebuilt-read",
        pdf_bytes
    )

    # Save job metadata so we can check it later
    job_record = {
        "job_id": job_id,
        "status": "running",
        "poller_url": poller._polling_method._polling_url
    }

    container.upload_blob(
        name=f"jobs/{job_id}.json",
        data=json.dumps(job_record),
        overwrite=True
    )

    # Respond immediately (no waiting!)
    return func.HttpResponse(
        json.dumps({"job_id": job_id}),
        mimetype="application/json"
    )

