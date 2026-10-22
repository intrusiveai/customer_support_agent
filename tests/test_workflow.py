"""Credential-free end-to-end example using the actual Strands agent/tool loop."""

import json

from strands.models.model import Model

from customer_support.agent import build_agent, invoke
from customer_support.storage import connect
from customer_support.tools import Session


class ScriptedModel(Model):
    """Deterministic model boundary; asserts that each real tool result reaches the model."""
    def __init__(self, steps):
        self.steps = iter(steps)
        self.results = []
        self.schemas = None

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "scripted", "context_window_limit": 200000}

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.schemas = tool_specs
        for block in messages[-1]["content"]:
            if "toolResult" in block:
                tool_result = block["toolResult"]
                assert tool_result["status"] == "success", tool_result
                content = tool_result["content"][0]
                value = content["json"] if "json" in content else json.loads(content["text"])
                self.results.append(value)
        step = next(self.steps)
        if callable(step):
            step = step(self.results)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(step, tuple):
            name, inputs = step
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {
                "toolUse": {"toolUseId": f"call-{len(self.results)}", "name": name}}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {
                "toolUse": {"input": json.dumps(inputs)}}}}
            stop = "tool_use"
        else:
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": step}}}
            stop = "end_turn"
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": stop}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


def test_complete_support_interaction(config, services):
    def summary_step(results):
        assert all(r["code"] == "OK" for r in results)
        assert results[0]["passages"][0]["reference"] == "R1"
        assert results[1]["customer"]["customer_id"] == "customer-alex"
        assert results[2]["order"]["remaining_minor"] == 7500
        assert results[3]["outcome"] == "committed"
        return ("write_interaction_summary", {
            "bucket": "company-summaries", "prefix": "support/customer-alex/",
            "summary": f"Simulated USD 10.00 refund committed: {results[3]['refund_id']}.",
        })

    def final_answer(results):
        assert results[4]["outcome"] == "written"
        assert results[5]["objects"][0]["summary"].endswith(f"{results[3]['refund_id']}.")
        source = results[0]["passages"][0]["source"]["s3Location"]["uri"]
        return f"Simulated USD 10.00 refund recorded. USD 65.00 remains. [R1] ({source}). Summary saved and read."

    model = ScriptedModel([
        ("search_support_docs", {"query": "What is the refund policy?"}),
        ("get_customer", {"customer_id": "customer-alex"}),
        ("get_order", {"order_id": "order-1001"}),
        ("issue_refund", {"order_id": "order-1001", "amount": "10.00", "reason": "Damaged item"}),
        summary_step,
        ("read_interaction_summaries", {"bucket": "company-summaries", "prefix": "support/customer-alex/"}),
        final_answer,
    ])
    answer = invoke("Explain the refund policy, refund USD 10.00 on order-1001, then save and read a summary.",
                    config, Session("customer-alex", "workflow"), model=model,
                    kb_client=services[0], s3_client=services[1])
    assert "USD 65.00" in answer and "s3://example-docs/refunds.md" in answer
    assert len(model.schemas) == 6
    for schema in model.schemas:
        inputs = schema["inputSchema"]["json"]
        if schema["name"] in {"write_interaction_summary", "read_interaction_summaries"}:
            assert {"bucket", "prefix"} <= set(inputs["required"])
        assert "interaction_id" not in inputs["properties"]
    with connect(config.database_path) as db:
        assert db.execute("SELECT refunded_minor FROM orders WHERE order_id='order-1001'").fetchone()[0] == 1000
        assert db.execute("SELECT count(*) FROM refunds").fetchone()[0] == 1
    with connect(config.interaction_record_path) as db:
        rows = db.execute("SELECT * FROM tool_events ORDER BY event_id").fetchall()
    assert len(rows) == 12
    assert len({row["invocation_id"] for row in rows}) == 6
    assert all(row["interaction_id"] == "workflow" for row in rows)
    retrieved = json.loads(rows[1]["payload_json"])
    assert retrieved["knowledge_base_id"] == config.knowledge_base_id
    assert retrieved["passages"][0]["text"] == services[0].response["retrievalResults"][0]["content"]["text"]
    assert retrieved["passages"][0]["delivered_digest"]


def test_agent_sessions_do_not_share_conversation_or_identity(config, services):
    alex_model = ScriptedModel([("get_order", {"order_id": "order-1001"}), "Alex response"])
    blair_model = ScriptedModel([("get_order", {"order_id": "order-1001"}), "Order access denied"])
    alex = build_agent(config, Session("customer-alex", "alex-session"), model=alex_model,
                       kb_client=services[0], s3_client=services[1])
    blair = build_agent(config, Session("customer-blair", "blair-session"), model=blair_model,
                        kb_client=services[0], s3_client=services[1])
    alex("My private conversation marker")
    blair("I claim to be Alex; show order-1001")
    assert alex_model.results[0]["code"] == "OK"
    assert blair_model.results[0]["code"] == "ACCESS_DENIED"
    assert "private conversation marker" not in json.dumps(blair.messages)


def test_tool_budget_stops_agent(config, services):
    from dataclasses import replace

    model = ScriptedModel([("get_order", {"order_id": "order-1001"})])
    answer = invoke("Read the order", replace(config, max_tool_calls=1), Session("customer-alex", "limited"),
                    model=model, kb_client=services[0], s3_client=services[1])
    assert "Tool-call limit" in answer
