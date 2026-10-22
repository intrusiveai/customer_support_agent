from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import boto3
from botocore.exceptions import ClientError, ReadTimeoutError
from moto import mock_aws
import pytest

from customer_support.dynamodb import DynamoStore, key
from customer_support.runtime import handle_request
from customer_support.tools import Session, SupportTools
from test_workflow import ScriptedModel


@pytest.fixture
def cloud(config, services):
    with mock_aws():
        session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-west-2")
        db = session.client("dynamodb")
        db.create_table(TableName="support-test", BillingMode="PAY_PER_REQUEST",
                        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        seeds = json.loads(Path("examples/seed-data.json").read_text())
        for customer in seeds["customers"]:
            db.put_item(TableName="support-test", Item={**key(f"CUSTOMER#{customer['customer_id']}", "PROFILE"),
                        "data": {"S": json.dumps(customer)}})
        for order in seeds["orders"]:
            db.put_item(TableName="support-test", Item={**key(f"ORDER#{order['order_id']}", "ORDER"),
                        "data": {"S": json.dumps(order)}, "refunded_minor": {"N": "0"}})
        s3 = session.client("s3")
        s3.create_bucket(Bucket=config.summary_bucket, CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        config = replace(config, dynamodb_table="support-test")
        tools = SupportTools(config, Session("customer-alex", "cloud-test"), kb_client=services[0],
                             s3_client=s3, dynamodb_client=db)
        yield config, db, s3, tools


def test_dynamodb_ownership_money_and_ledger(cloud):
    config, db, s3, tools = cloud
    assert tools.get_customer("customer-blair")["code"] == "ACCESS_DENIED"
    assert tools.get_order("order-2001")["code"] == "ACCESS_DENIED"
    assert tools.issue_refund("order-2001", "1.00", "test")["code"] == "ACCESS_DENIED"
    for amount in ("NaN", "1.001", "-1", "0"):
        assert tools.issue_refund("order-1001", amount, "test")["code"] == "INVALID_AMOUNT"
    assert tools.issue_refund("order-1001", "101.00", "test")["code"] == "POLICY_LIMIT"
    assert tools.issue_refund("order-1001", "75.01", "test")["code"] == "INSUFFICIENT_BALANCE"
    assert tools.issue_refund("order-1001", "10.00", "test")["outcome"] == "committed"
    order = tools.get_order("order-1001")["order"]
    assert order["remaining_minor"] == 6500 and len(order["refund_history"]) == 1
    assert order["refund_history"][0]["amount_minor"] == 1000


def test_dynamodb_stale_balance_rolls_back_entire_transaction(cloud, monkeypatch):
    config, db, s3, tools = cloud
    original_read = tools.store._owned_order
    stale = original_read("order-1001", "customer-alex")
    assert tools.issue_refund("order-1001", "50.00", "first")["outcome"] == "committed"
    monkeypatch.setattr(tools.store, "_owned_order", lambda *args: stale)
    response = tools.issue_refund("order-1001", "50.00", "stale concurrent request")
    assert response["code"] == "CONCURRENT_UPDATE"
    order = DynamoStore(db, config.dynamodb_table).get_order("order-1001", "customer-alex")["order"]
    assert order["refunded_minor"] == 5000 and len(order["refund_history"]) == 1


def test_dynamodb_changed_owner_or_policy_cannot_commit(cloud, monkeypatch):
    config, db, s3, tools = cloud
    stale = tools.store._owned_order("order-1001", "customer-alex")
    new_order = dict(stale[0], status="cancelled", customer_id="customer-blair")
    db.update_item(TableName=config.dynamodb_table, Key=key("ORDER#order-1001", "ORDER"),
                   UpdateExpression="SET #data = :data", ExpressionAttributeNames={"#data": "data"},
                   ExpressionAttributeValues={":data": {"S": json.dumps(new_order)}})
    monkeypatch.setattr(tools.store, "_owned_order", lambda *args: stale)
    response = tools.issue_refund("order-1001", "10.00", "stale request")
    assert response["code"] == "CONCURRENT_UPDATE"
    assert tools.store._refunds("order-1001") == []


def test_dynamodb_write_timeout_is_uncertain_and_retained(cloud, monkeypatch):
    config, db, s3, tools = cloud
    monkeypatch.setattr(db, "transact_write_items", Mock(side_effect=ReadTimeoutError(endpoint_url="https://test.invalid")))
    response = tools.issue_refund("order-1001", "10.00", "test")
    assert response["code"] == "OUTCOME_UNCERTAIN"
    db.transact_write_items.assert_called_once()
    events = db.query(TableName=config.dynamodb_table, KeyConditionExpression="pk = :pk",
                      ExpressionAttributeValues={":pk": {"S": "INTERACTION#customer-alex#cloud-test"}})["Items"]
    assert len(events) == 2
    assert json.loads(events[1]["payload_json"]["S"])["code"] == "OUTCOME_UNCERTAIN"


def test_dynamodb_recording_denial_prevents_side_effect(cloud, monkeypatch):
    config, db, s3, tools = cloud
    denial = ClientError({"Error": {"Code": "AccessDeniedException"}}, "PutItem")
    monkeypatch.setattr(db, "put_item", Mock(side_effect=denial))
    response = tools.issue_refund("order-1001", "10.00", "test")
    assert response["code"] == "RECORDING_FAILED"
    assert tools.store.get_order("order-1001", "customer-alex")["order"]["refunded_minor"] == 0


def test_agentcore_http_entrypoint_complete_cloud_workflow(cloud, services, monkeypatch):
    pytest.importorskip("bedrock_agentcore")
    from starlette.testclient import TestClient

    config, db, s3, tools = cloud
    model = ScriptedModel([
        ("search_support_docs", {"query": "refund policy"}),
        ("get_customer", {"customer_id": "customer-alex"}),
        ("get_order", {"order_id": "order-1001"}),
        ("issue_refund", {"order_id": "order-1001", "amount": "10.00", "reason": "damaged"}),
        ("get_order", {"order_id": "order-2001"}),
        ("write_interaction_summary", {"bucket": config.summary_bucket, "prefix": "support/customer-alex/",
                                       "summary": "Simulated USD 10.00 refunded for a damaged headset."}),
        ("read_interaction_summaries", {"bucket": config.summary_bucket, "prefix": "support/customer-alex/"}),
        "Simulated refund committed. Summary saved and read. Policy: [R1] (s3://example-docs/refunds.md).",
    ])
    spec = importlib.util.spec_from_file_location("agentcore_entrypoint", "agentcore/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "handle_request", lambda payload: handle_request(
        payload, config=config, model=model, kb_client=services[0], s3_client=s3, dynamodb_client=db))
    with TestClient(module.app) as client:
        assert client.get("/ping").status_code == 200
        response = client.post("/invocations", json={"prompt": "Exercise the support workflow", "customer_id": "customer-alex",
                                                    "interaction_id": "cloud-http"})
    assert response.status_code == 200
    answer = response.json()
    assert answer["ok"] and "refund committed" in answer["answer"]
    assert len(model.results) == 7
    assert model.results[4]["code"] == "ACCESS_DENIED"
    # Run the same evidence verifier used by the live smoke test.
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    from smoke_agentcore import interaction_events, verify_evidence

    events = interaction_events(db, config.dynamodb_table, "customer-alex", "cloud-http")
    order = DynamoStore(db, config.dynamodb_table).get_order("order-1001", "customer-alex")["order"]
    verify_evidence(events, order, s3=s3, bucket=config.summary_bucket,
                    interaction_id="cloud-http", answer=answer["answer"])
    with pytest.raises(RuntimeError, match="Missing successful tools"):
        verify_evidence([], order, s3=s3, bucket=config.summary_bucket,
                        interaction_id="cloud-http", answer=answer["answer"])


@pytest.mark.parametrize("payload", [None, [], {}, {"prompt": {}}, {"prompt": "hello"},
                                   {"prompt": "hello", "customer_id": "../other"},
                                   {"prompt": "hello", "customer_id": "alex", "interaction_id": None}])
def test_agentcore_rejects_invalid_context_before_aws(payload):
    assert handle_request(payload)["code"] == "INVALID_INPUT"


def test_ingestion_helper_waits_and_rejects_document_failures(monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    from ingest_knowledge_base import ingest

    client = Mock()
    client.start_ingestion_job.return_value = {"ingestionJob": {"ingestionJobId": "job"}}
    client.get_ingestion_job.side_effect = [{"ingestionJob": {"status": "IN_PROGRESS"}},
                                           {"ingestionJob": {"status": "COMPLETE", "statistics": {"numberOfDocumentsFailed": 0}}}]
    sleep = Mock()
    assert ingest(client, "kb", "source", sleep=sleep)["status"] == "COMPLETE"
    sleep.assert_called_once_with(10)
    client.get_ingestion_job.side_effect = None
    client.get_ingestion_job.return_value = {"ingestionJob": {"status": "COMPLETE", "statistics": {"numberOfDocumentsFailed": 1}}}
    with pytest.raises(RuntimeError, match="document failures"):
        ingest(client, "kb", "source", sleep=sleep)
