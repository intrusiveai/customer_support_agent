"""Exercise all six live tools and verify persisted evidence, not just the answer text."""

import json
import sys
from uuid import uuid4

import boto3
from botocore.config import Config

from customer_support.dynamodb import DynamoStore, key
from customer_support.tools import digest

from invoke_agentcore import deployment_outputs, invoke_deployed


def interaction_events(client, table, customer_id, interaction_id):
    events, cursor = [], {}
    while True:
        page = client.query(
            TableName=table, ConsistentRead=True, KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": {"S": f"INTERACTION#{customer_id}#{interaction_id}"}}, **cursor,
        )
        events.extend({"phase": item["phase"]["S"], "tool_name": item["tool_name"]["S"],
                       "invocation_id": item["invocation_id"]["S"],
                       "payload": json.loads(item["payload_json"]["S"])} for item in page["Items"])
        if not page.get("LastEvaluatedKey"):
            return events
        cursor = {"ExclusiveStartKey": page["LastEvaluatedKey"]}


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify_evidence(events, order, *, s3, bucket, interaction_id, answer):
    started = {e["invocation_id"]: e for e in events if e["phase"] == "started"}
    completed = [e for e in events if e["phase"] == "completed"]
    required = {"search_support_docs", "get_customer", "get_order", "issue_refund",
                "write_interaction_summary", "read_interaction_summaries"}
    successes = {e["tool_name"] for e in completed if e["payload"]["code"] == "OK"}
    check(required <= successes, f"Missing successful tools: {sorted(required - successes)}")
    denied = [e for e in completed if e["tool_name"] == "get_order" and e["payload"]["code"] == "ACCESS_DENIED"]
    check(any(started[e["invocation_id"]]["payload"]["order_id"] == "order-2001" for e in denied),
          "Cross-customer order access denial was not exercised")
    check(order["refunded_minor"] == 1000 and len(order["refund_history"]) == 1,
          "Expected exactly one USD 10.00 refund in the durable order ledger")
    refund = order["refund_history"][0]
    check(refund["interaction_id"] == interaction_id, "Refund was not associated with this interaction")
    retrieved = [e["payload"] for e in completed if e["tool_name"] == "search_support_docs"]
    passages = [p for r in retrieved for p in r.get("passages", []) if p.get("text")]
    check(any(p.get("source") and p.get("content_digest") for p in passages), "No retrieved passage evidence")
    sources = [p.get("source", {}).get("s3Location", {}).get("uri") for p in passages]
    check(any(source and source in answer for source in sources), "Answer did not cite a retrieved source URI")
    writes = [e["payload"] for e in completed if e["tool_name"] == "write_interaction_summary"
              and e["payload"].get("outcome") == "written"]
    reads = [obj for e in completed if e["tool_name"] == "read_interaction_summaries"
             for obj in e["payload"].get("objects", []) if obj.get("code") == "OK"]
    for written in writes:
        check(written["bucket"] == bucket, "Summary was written outside the expected smoke-test bucket")
        obj = s3.get_object(Bucket=bucket, Key=written["object_key"])
        try:
            raw = obj["Body"].read()
        finally:
            obj["Body"].close()
        check(digest(raw) == written["content_digest"], "Stored summary digest mismatch")
        check(json.loads(raw)["interaction_id"] == interaction_id, "Wrong summary interaction")
        check(any(r["object_key"] == written["object_key"] and r["content_digest"] == written["content_digest"]
                  for r in reads), "Agent did not read back its saved summary")


def main():
    outputs = deployment_outputs()
    aws_config = Config(connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 1})
    db = boto3.client("dynamodb", region_name=outputs["region"], config=aws_config)
    s3 = boto3.client("s3", region_name=outputs["region"], config=aws_config)
    store = DynamoStore(db, outputs["dynamodb_table"])
    interaction_id = f"smoke-{uuid4()}"
    order_id = f"smoke-order-{uuid4()}"
    # Confirm the Terraform seeds exist, then add a fresh order instead of resetting balances.
    for customer, order in (("customer-alex", "order-1001"), ("customer-blair", "order-2001")):
        check(store.get_customer(customer, customer)["ok"], f"Missing seeded customer {customer}")
        check(store.get_order(order, customer)["ok"], f"Missing seeded order {order}")
    order = {"order_id": order_id, "customer_id": "customer-alex", "total_minor": 7500,
             "currency": "USD", "status": "delivered", "items": [{"sku": "SMOKE-HEADSET",
             "name": "Smoke-test headset", "quantity": 1, "unit_price_minor": 7500}]}
    db.put_item(TableName=outputs["dynamodb_table"], Item={**key(f"ORDER#{order_id}", "ORDER"),
                "data": {"S": json.dumps(order)}, "refunded_minor": {"N": "0"}},
                ConditionExpression="attribute_not_exists(pk)")
    prefix = outputs["runtime_config"]["summary_write_prefix"].replace(
        "{customer_id}", "customer-alex").replace("{interaction_id}", interaction_id)
    print(f"Created test order {order_id}. Records will be retained.", flush=True)
    prompt = (
        f"Please complete this support test in order. Retrieve and explain the refund policy, citing its actual source URI. "
        f"Read my customer-alex profile and order {order_id}. Issue exactly one simulated USD 10.00 refund on "
        f"that order for a damaged headset. Also request order-2001 so you can report the ownership denial. "
        f"I am closing this interaction: save a summary to bucket {outputs['summary_bucket']} under prefix {prefix}, "
        f"then read summaries under that exact prefix to verify the save. Report the source URI and outcomes."
    )
    response = invoke_deployed(outputs, prompt, "customer-alex", interaction_id=interaction_id)
    print(response["answer"])
    events = interaction_events(db, outputs["dynamodb_table"], "customer-alex", interaction_id)
    order = store.get_order(order_id, "customer-alex")["order"]
    verify_evidence(events, order, s3=s3, bucket=outputs["summary_bucket"],
                    interaction_id=interaction_id, answer=response["answer"])
    print(f"PASS: all six tools, citation, ownership denial, committed refund, and summary round trip. Interaction {interaction_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
