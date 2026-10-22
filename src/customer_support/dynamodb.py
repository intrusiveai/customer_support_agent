"""Durable records for AgentCore, using one DynamoDB table with pk/sk keys."""

import json
from uuid import uuid4

from botocore.exceptions import ClientError

from .storage import money, refund_amount, result, utc_now, validate_refund_input


def key(pk: str, sk: str) -> dict:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


class DynamoStore:
    def __init__(self, client, table: str):
        self.client, self.table = client, table

    def _item(self, pk: str, sk: str) -> dict | None:
        return self.client.get_item(TableName=self.table, Key=key(pk, sk),
                                    ConsistentRead=True).get("Item")

    def get_customer(self, customer_id: str, active_customer: str) -> dict:
        if customer_id != active_customer:
            return result("ACCESS_DENIED", "Customer is outside the active session.")
        item = self._item(f"CUSTOMER#{customer_id}", "PROFILE")
        if not item:
            return result("NOT_FOUND", "Customer not found.")
        return result(customer=json.loads(item["data"]["S"]))

    def _owned_order(self, order_id, active_customer):
        item = self._item(f"ORDER#{order_id}", "ORDER")
        if not item:
            return None, None, result("NOT_FOUND", "Order not found.")
        order = json.loads(item["data"]["S"])
        if order["customer_id"] != active_customer:
            return None, None, result("ACCESS_DENIED", "Order is outside the active session.")
        order["refunded_minor"] = int(item["refunded_minor"]["N"])
        return order, item, None

    def _refunds(self, order_id):
        history, cursor = [], {}
        while True:
            page = self.client.query(
                TableName=self.table, ConsistentRead=True,
                KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                ExpressionAttributeValues={":pk": {"S": f"ORDER#{order_id}"},
                                           ":prefix": {"S": "REFUND#"}}, **cursor,
            )
            history.extend(json.loads(item["data"]["S"]) for item in page["Items"])
            if not page.get("LastEvaluatedKey"):
                return sorted(history, key=lambda r: (r["created_at"], r["refund_id"]))
            cursor = {"ExclusiveStartKey": page["LastEvaluatedKey"]}

    def get_order(self, order_id: str, active_customer: str) -> dict:
        # Refund totals only increase. Re-read the order around the history query
        # so a concurrent refund cannot produce a mismatched balance and history.
        for _ in range(3):
            order, before, error = self._owned_order(order_id, active_customer)
            if error:
                return error
            history = self._refunds(order_id)
            after = self._item(f"ORDER#{order_id}", "ORDER")
            if before == after:
                break
        else:
            return result("CONCURRENT_UPDATE", "Order changed during the read; please read it again.")
        customer_result = self.get_customer(active_customer, active_customer)
        if not customer_result["ok"]:
            return result("STORAGE_FAILURE", "Order's customer profile is missing.")
        customer = customer_result["customer"]
        order.update(total=money(order["total_minor"], order["currency"]),
                     remaining_minor=order["total_minor"] - order["refunded_minor"],
                     refund_history=history, shipping_name=customer["name"],
                     shipping_address=customer["address"])
        return result(order=order)

    def issue_refund(self, order_id, amount, reason, session, invocation_id, config) -> dict:
        error = validate_refund_input(amount, reason)
        if error:
            return error
        order, original, error = self._owned_order(order_id, session.customer_id)
        if error:
            return error
        minor, error = refund_amount(order, amount, config)
        if error:
            return error
        refund_id = str(uuid4())
        refund = {"refund_id": refund_id, "order_id": order_id, "amount_minor": minor,
                  "currency": order["currency"], "reason": reason.strip(),
                  "customer_id": session.customer_id, "interaction_id": session.interaction_id,
                  "invocation_id": invocation_id, "created_at": utc_now()}
        try:
            self.client.transact_write_items(ClientRequestToken=invocation_id, TransactItems=[
                {"Update": {
                    "TableName": self.table, "Key": key(f"ORDER#{order_id}", "ORDER"),
                    "UpdateExpression": "SET refunded_minor = refunded_minor + :amount",
                    "ConditionExpression": "#data = :original AND refunded_minor = :before",
                    "ExpressionAttributeNames": {"#data": "data"},
                    "ExpressionAttributeValues": {":original": original["data"],
                                                  ":before": original["refunded_minor"],
                                                  ":amount": {"N": str(minor)}},
                }},
                {"Put": {
                    "TableName": self.table,
                    "Item": {**key(f"ORDER#{order_id}", f"REFUND#{invocation_id}"),
                             "data": {"S": json.dumps(refund)}},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }},
            ])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                # DynamoDB rejected the transaction atomically. Do not retry a refund.
                reasons = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
                return result("CONCURRENT_UPDATE" if "ConditionalCheckFailed" in reasons else "TRANSACTION_REJECTED",
                              "Refund transaction was not committed. Inspect the order before a new request.",
                              outcome="rejected", aws_error_code="TransactionCanceledException",
                              aws_request_id=exc.response.get("ResponseMetadata", {}).get("RequestId"))
            raise
        remaining = order["total_minor"] - order["refunded_minor"] - minor
        return result(refund_id=refund_id, order_id=order_id, amount_minor=minor,
                      amount=money(minor, order["currency"]), currency=order["currency"],
                      refunded_minor=order["refunded_minor"] + minor, remaining_minor=remaining,
                      remaining_balance=money(remaining, order["currency"]),
                      outcome="committed", simulated=True)


class DynamoInteractionRecords:
    def __init__(self, client, table: str):
        self.client, self.table = client, table

    def append(self, session, invocation_id, tool_name, phase, payload):
        timestamp = utc_now()
        self.client.put_item(
            TableName=self.table,
            Item={**key(f"INTERACTION#{session.customer_id}#{session.interaction_id}",
                        f"{timestamp}#{invocation_id}#{phase}"),
                  "invocation_id": {"S": invocation_id}, "customer_id": {"S": session.customer_id},
                  "interaction_id": {"S": session.interaction_id}, "tool_name": {"S": tool_name},
                  "phase": {"S": phase}, "created_at": {"S": timestamp},
                  "payload_json": {"S": json.dumps(payload, ensure_ascii=False)}},
            ConditionExpression="attribute_not_exists(pk)",
        )
