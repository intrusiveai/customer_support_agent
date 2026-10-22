"""AgentCore payload adapter. IAM-authorized callers supply trusted session context."""

import json
import os
from uuid import uuid4

from .agent import invoke
from .config import Config
from .tools import Session


def handle_request(payload: dict, *, config: Config | None = None, **dependencies) -> dict:
    """One independent support request, with durable records but no shared conversation.

    The runtime's IAM permission is for trusted operators/backend services. A public
    frontend must authenticate its user before its backend supplies customer_id.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "code": "INVALID_INPUT", "message": "Expected a JSON object."}
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        return {"ok": False, "code": "INVALID_INPUT", "message": "prompt must contain 1–16000 characters."}
    try:
        session = Session(payload.get("customer_id"), payload.get("interaction_id", str(uuid4())))
    except ValueError as exc:
        return {"ok": False, "code": "INVALID_INPUT", "message": str(exc)}
    try:
        if config is None:
            config = Config(**json.loads(os.environ["SUPPORT_AGENT_CONFIG_JSON"]))
        answer = invoke(prompt, config, session, **dependencies)
        return {"ok": True, "code": "OK", "answer": answer,
                "customer_id": session.customer_id, "interaction_id": session.interaction_id}
    except Exception as exc:
        # Do not expose SDK exception strings or imply that earlier tools were rolled back.
        return {"ok": False, "code": "AGENT_FAILURE", "error_type": type(exc).__name__,
                "interaction_id": session.interaction_id,
                "message": "Agent invocation failed. Inspect retained tool records and the ledger before retrying writes."}
