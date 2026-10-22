from dataclasses import replace
from io import BytesIO

import pytest

from customer_support.config import Config
from customer_support.storage import initialize, seed_demo
from customer_support.tools import Session, SupportTools


class MemoryS3:
    """AWS boundary double. Does not replace the application's storage or tool logic."""
    def __init__(self):
        self.objects = {}
        self.requests = []

    def put_object(self, **kwargs):
        self.requests.append(("put", kwargs))
        key = (kwargs["Bucket"], kwargs["Key"])
        assert kwargs["IfNoneMatch"] == "*"
        assert key not in self.objects
        self.objects[key] = kwargs["Body"]
        return {"ResponseMetadata": {"RequestId": "put-request"}}

    def list_objects_v2(self, **kwargs):
        self.requests.append(("list", kwargs))
        keys = sorted(key for bucket, key in self.objects
                      if bucket == kwargs["Bucket"] and key.startswith(kwargs["Prefix"]))
        return {"Contents": [{"Key": key, "Size": len(self.objects[(kwargs["Bucket"], key)])}
                             for key in keys[:kwargs["MaxKeys"]]],
                "IsTruncated": len(keys) > kwargs["MaxKeys"],
                "ResponseMetadata": {"RequestId": "list-request"}}

    def get_object(self, **kwargs):
        self.requests.append(("get", kwargs))
        payload = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {"Body": BytesIO(payload), "ContentLength": len(payload),
                "ResponseMetadata": {"RequestId": "get-request"}}


class MemoryKB:
    def __init__(self):
        self.requests = []
        self.response = {"retrievalResults": [{
            "content": {"type": "TEXT", "text": "Delivered orders may receive simulated refunds within the remaining balance."},
            "location": {"type": "S3", "s3Location": {"uri": "s3://example-docs/refunds.md"}},
            "metadata": {"title": "Refund policy"}, "score": 0.91,
        }]}

    def retrieve(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


@pytest.fixture
def config(tmp_path):
    value = Config(model_id="test-model", knowledge_base_id="TESTKB1234",
                   summary_bucket="company-summaries",
                   database_path=str(tmp_path / "support.sqlite3"),
                   interaction_record_path=str(tmp_path / "interactions.sqlite3"))
    initialize(value.database_path, value.interaction_record_path)
    seed_demo(value.database_path)
    return value


@pytest.fixture
def services():
    return MemoryKB(), MemoryS3()


@pytest.fixture
def make_tools(config, services):
    def make(customer="customer-alex", **overrides):
        return SupportTools(replace(config, **overrides), Session(customer, "test-interaction"),
                            kb_client=services[0], s3_client=services[1])
    return make
