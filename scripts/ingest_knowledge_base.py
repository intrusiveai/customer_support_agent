"""Start sample-document ingestion and wait for a verifiable successful result."""

import os
import time

import boto3
from botocore.config import Config


def ingest(client, knowledge_base_id, data_source_id, *, timeout=600, sleep=time.sleep):
    response = client.start_ingestion_job(knowledgeBaseId=knowledge_base_id, dataSourceId=data_source_id)
    job_id = response["ingestionJob"]["ingestionJobId"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get_ingestion_job(knowledgeBaseId=knowledge_base_id, dataSourceId=data_source_id,
                                       ingestionJobId=job_id)["ingestionJob"]
        status = job["status"]
        print(f"Ingestion {job_id}: {status}", flush=True)
        if status == "COMPLETE":
            if job.get("statistics", {}).get("numberOfDocumentsFailed", 0):
                raise RuntimeError(f"Ingestion completed with document failures: {job.get('failureReasons', [])}")
            return job
        if status in {"FAILED", "STOPPED"}:
            raise RuntimeError(f"Ingestion failed: {job.get('failureReasons', [])}")
        sleep(10)
    raise TimeoutError(f"Ingestion {job_id} did not finish within {timeout}s; inspect the job before retrying.")


if __name__ == "__main__":
    client = boto3.client("bedrock-agent", region_name=os.environ["SUPPORT_AWS_REGION"],
                          config=Config(connect_timeout=5, read_timeout=30,
                                        retries={"total_max_attempts": 1}))
    ingest(client, os.environ["SUPPORT_KB_ID"], os.environ["SUPPORT_DATA_SOURCE_ID"])
