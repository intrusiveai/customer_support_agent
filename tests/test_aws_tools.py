import json
from unittest.mock import Mock

import boto3
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from io import BytesIO
import pytest

from customer_support.agent import aws_config
from customer_support.tools import NARRATIVE_KIND, Session, SupportTools, digest


def error(code="AccessDenied", status=403):
    return ClientError({"Error": {"Code": code, "Message": "not retained"},
                        "ResponseMetadata": {"RequestId": "request-123", "HTTPStatusCode": status}}, "Test")


def test_retrieval_truncation_retains_sources_and_digests(make_tools, services):
    tools = make_tools(retrieval_text_chars=5)
    response = tools.search_support_docs("refunds")
    passage = response["passages"][0]
    assert response["code"] == "PARTIAL"
    assert passage["text"] == "Deliv" and passage["truncated"]
    assert passage["content_digest"] != passage["delivered_digest"] == digest("Deliv")
    assert passage["source"]["s3Location"]["uri"] == "s3://example-docs/refunds.md"
    assert services[0].requests[0]["knowledgeBaseId"] == tools.config.knowledge_base_id


def test_retrieval_empty_unsupported_mixed_and_pagination(make_tools, services):
    kb = services[0]
    tools = make_tools()
    text = kb.response["retrievalResults"][0]
    kb.response = {"retrievalResults": []}
    assert tools.search_support_docs("q")["code"] == "EMPTY"
    image = {"content": {"type": "IMAGE", "byteContent": "data:image/png;base64,AAAA"}}
    kb.response = {"retrievalResults": [image]}
    assert tools.search_support_docs("q")["code"] == "UNSUPPORTED_CONTENT"
    kb.response = {"retrievalResults": [text, image], "nextToken": "next"}
    response = tools.search_support_docs("q")
    assert response["code"] == "PARTIAL" and response["incomplete"]
    assert response["passages"][1]["code"] == "UNSUPPORTED_CONTENT"


@pytest.mark.parametrize("aws_code,status,code", [
    ("AccessDeniedException", 403, "ACCESS_DENIED"),
    ("InternalServerException", 500, "SERVICE_FAILURE"),
])
def test_retrieval_failures_are_not_empty(make_tools, services, aws_code, status, code):
    services[0].retrieve = Mock(side_effect=error(aws_code, status))
    response = make_tools().search_support_docs("refunds")
    assert response["code"] == code
    assert response["aws_error_code"] == aws_code and response["aws_request_id"] == "request-123"
    assert response["knowledge_base_id"] == "TESTKB1234"
    assert "not retained" not in json.dumps(response)


def test_model_supplied_s3_locations_are_preserved_and_objects_not_overwritten(make_tools, services):
    tools = make_tools()
    first = tools.write_interaction_summary("outside-company-bucket", "any/customer", "First narrative")
    second = tools.write_interaction_summary("outside-company-bucket", "any/customer", "Second narrative")
    assert first["object_key"] != second["object_key"]
    assert first["object_key"].startswith("any/customer/")
    stored = json.loads(services[1].objects[(first["bucket"], first["object_key"])])
    assert stored["customer_id"] == "customer-alex"
    assert stored["content_kind"] == NARRATIVE_KIND
    assert first["content_digest"] == digest(services[1].objects[(first["bucket"], first["object_key"])])
    # Another session may read all summaries under this broad prefix by design.
    response = make_tools("customer-blair").read_interaction_summaries("outside-company-bucket", "")
    assert len(response["objects"]) == 2 and response["untrusted"]
    assert services[1].requests[-3][1]["Prefix"] == ""
    assert response["objects"][0]["claimed_metadata"]["customer_id"] == "customer-alex"


def test_s3_listing_download_text_and_malformed_limits(make_tools, services):
    tools = make_tools()
    for i in range(3):
        tools.write_interaction_summary("test-bucket", "all/", f"Summary number {i}")
    response = make_tools(s3_list_limit=1).read_interaction_summaries("test-bucket", "all/")
    assert len(response["objects"]) == 1
    assert response["incomplete_listing"] and response["code"] == "PARTIAL"
    response = make_tools(s3_returned_text_chars=5).read_interaction_summaries("test-bucket", "all/")
    assert sum(len(o["summary"]) for o in response["objects"]) == 5
    assert response["truncated"]
    response = make_tools(s3_download_bytes=10).read_interaction_summaries("test-bucket", "all/")
    assert response["downloaded_bytes"] == 0 and response["truncated"]
    assert all(o["code"] == "LIMIT_EXCEEDED" for o in response["objects"])
    services[1].objects[("test-bucket", "broken/a.json")] = b"not json"
    services[1].objects[("test-bucket", "broken/b.json")] = b'{"summary":"invented refund"}'
    response = tools.read_interaction_summaries("test-bucket", "broken/")
    assert response["code"] == "PARTIAL"
    assert all(o["code"] == "MALFORMED_OBJECT" and o["content_digest"] for o in response["objects"])
    assert tools.read_interaction_summaries("test-bucket", "absent/")["code"] == "EMPTY"


def test_uncertain_write_retains_destination_digest_and_does_not_retry(make_tools, services):
    services[1].put_object = Mock(side_effect=ReadTimeoutError(endpoint_url="https://example.invalid"))
    response = make_tools().write_interaction_summary("test-bucket", "all/", "A narrative")
    assert response["code"] == "OUTCOME_UNCERTAIN" and response["outcome"] == "uncertain"
    assert response["bucket"] == "test-bucket" and response["object_key"].startswith("all/")
    assert response["content_digest"]
    assert services[1].put_object.call_count == 1


def test_s3_denial_is_explicit_for_read_and_write(make_tools, services):
    services[1].put_object = Mock(side_effect=error())
    services[1].list_objects_v2 = Mock(side_effect=error())
    tools = make_tools()
    for response in (tools.write_interaction_summary("test-bucket", "", "A narrative"),
                     tools.read_interaction_summaries("test-bucket", "")):
        assert response["code"] == "ACCESS_DENIED"
        assert response["aws_request_id"] == "request-123"


def test_s3_per_object_failure_keeps_successes(make_tools, services):
    tools = make_tools()
    tools.write_interaction_summary("test-bucket", "all/", "A narrative")
    services[1].get_object = Mock(side_effect=error())
    response = tools.read_interaction_summaries("test-bucket", "all/")
    assert response["code"] == "PARTIAL" and response["objects"][0]["code"] == "ACCESS_DENIED"


def test_failed_stream_read_consumes_budget_and_closes_body(make_tools, services):
    tools = make_tools()
    for _ in range(2):
        tools.write_interaction_summary("test-bucket", "all/", "A narrative")
    body = Mock()
    body.read.side_effect = ReadTimeoutError(endpoint_url="https://example.invalid")
    services[1].get_object = Mock(return_value={"Body": body, "ContentLength": 400})
    response = make_tools(s3_download_bytes=1024).read_interaction_summaries("test-bucket", "all/")
    body.close.assert_called_once()
    services[1].get_object.assert_called_once()
    assert response["downloaded_bytes"] == 1024 and response["downloaded_bytes_is_upper_bound"]
    assert response["objects"][1]["code"] == "LIMIT_EXCEEDED"


def test_changed_object_and_elapsed_deadline_are_explicit(make_tools, services, monkeypatch):
    tools = make_tools()
    tools.write_interaction_summary("test-bucket", "all/", "A narrative")
    body = BytesIO(b"x" * 1000)
    services[1].get_object = Mock(return_value={"Body": body, "ContentLength": 1000})
    response = make_tools(s3_object_bytes=500).read_interaction_summaries("test-bucket", "all/")
    assert response["objects"][0]["partial_content_digest"] == digest(b"x" * 500)
    assert response["truncated"] and body.closed
    monkeypatch.setattr("customer_support.tools.time.monotonic", Mock(side_effect=[0, 100]))
    services[1].get_object.reset_mock()
    response = tools.read_interaction_summaries("test-bucket", "all/")
    assert response["objects"][0]["code"] == "LIMIT_EXCEEDED"
    services[1].get_object.assert_not_called()


def test_put_5xx_is_uncertain_and_size_rejections_do_not_write(make_tools, services):
    services[1].put_object = Mock(side_effect=error("InternalError", 500))
    response = make_tools().write_interaction_summary("test-bucket", "", "narrative")
    assert response["code"] == "OUTCOME_UNCERTAIN"
    services[1].put_object.reset_mock()
    assert make_tools(summary_max_chars=2).write_interaction_summary("test-bucket", "", "long")["code"] == "INVALID_INPUT"
    assert make_tools(s3_object_bytes=10).write_interaction_summary("test-bucket", "", "x")["code"] == "LIMIT_EXCEEDED"
    services[1].put_object.assert_not_called()


def test_boto_contracts_for_retrieve_and_all_s3_operations(config):
    # Real SDK request/response shape checks; credentials are inert placeholders and no network is used.
    session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-west-2")
    kb = session.client("bedrock-agent-runtime", config=aws_config(config))
    s3 = session.client("s3", config=aws_config(config))
    tools = SupportTools(config, Session("customer-alex", "sdk-contract"), kb_client=kb, s3_client=s3)
    body = json.dumps({"schema_version": 1, "summary_id": "test", "created_at": "2026-01-01T00:00:00Z",
                       "customer_id": "untrusted-other-customer", "interaction_id": "test",
                       "content_kind": NARRATIVE_KIND, "summary": "A narrative"}).encode()
    with Stubber(kb) as kb_stub, Stubber(s3) as s3_stub:
        kb_stub.add_response("retrieve", {"retrievalResults": [{"content": {"text": "Policy", "type": "TEXT"},
                             "location": {"type": "WEB", "webLocation": {"url": "https://example.com/policy"}},
                             "score": 0.5, "metadata": {"title": "Policy"}}]}, {
            "knowledgeBaseId": config.knowledge_base_id, "retrievalQuery": {"text": "policy"},
            "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 5}},
        })
        s3_stub.add_response("put_object", {}, {"Bucket": "alternate-bucket", "Key": ANY, "Body": ANY,
                                              "ContentType": "application/json", "IfNoneMatch": "*"})
        s3_stub.add_response("list_objects_v2", {"Contents": [{"Key": "broad/test.json", "Size": len(body)}],
                                                 "IsTruncated": False},
                             {"Bucket": "alternate-bucket", "Prefix": "broad/", "MaxKeys": 10})
        s3_stub.add_response("get_object", {"Body": StreamingBody(BytesIO(body), len(body)), "ContentLength": len(body)},
                             {"Bucket": "alternate-bucket", "Key": "broad/test.json"})
        assert tools.search_support_docs("policy")["code"] == "OK"
        assert tools.write_interaction_summary("alternate-bucket", "other/", "narrative")["outcome"] == "written"
        response = tools.read_interaction_summaries("alternate-bucket", "broad/")
        assert response["objects"][0]["claimed_metadata"]["customer_id"] == "untrusted-other-customer"
        kb_stub.assert_no_pending_responses()
        s3_stub.assert_no_pending_responses()
    assert s3.meta.config.retries["total_max_attempts"] == 1
