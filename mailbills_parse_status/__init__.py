import azure.functions as func
import json
import os
from azure.storage.blob import BlobServiceClient
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

def main(req: func.HttpRequest) -> func.HttpResponse:
    job_id = req.params.get("job_id")
    if not job_id:
        return func.HttpResponse(
            "Missing job_id",
            status_code=400
        )

    blob_service = BlobServiceClient.from_connection_string(
        os.environ["AzureWebJobsStorage"]
    )
    container = blob_service.get_container_client("mailbills")

    job_blob = container.get_blob_client(f"jobs/{job_id}.json")
    job_data = json.loads(job_blob.download_blob().readall())

    if job_data["status"] == "done":
        text_blob = container.get_blob_client(f"results/{job_id}.txt")
        text = text_blob.download_blob().readall().decode("utf-8")
        return func.HttpResponse(
            json.dumps({"status": "done", "text": text}),
            mimetype="application/json"
        )

    # Reconnect to OCR poller
    client = DocumentAnalysisClient(
        endpoint=os.environ["AZURE_DOCINTEL_ENDPOINT"],
        credential=AzureKeyCredential(os.environ["AZURE_DOCINTEL_KEY"])
    )

    poller = client.begin_analyze_document_from_url(
        "prebuilt-read",
        job_data["poller_url"]
    )

    if poller.done():
        result = poller.result()
        text = "\n".join(
            line.content
            for page in result.pages
            for line in page.lines
        )

        container.upload_blob(
            name=f"results/{job_id}.txt",
            data=text,
            overwrite=True
        )

        job_data["status"] = "done"
        job_blob.upload_blob(json.dumps(job_data), overwrite=True)

        return func.HttpResponse(
            json.dumps({"status": "done", "text": text}),
            mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps({"status": "running"}),
        mimetype="application/json"
    )

