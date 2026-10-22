"""Invoke the Terraform-deployed support agent with the existing AWS credential chain."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]


def deployment_outputs():
    raw = subprocess.check_output(["terraform", f"-chdir={ROOT / 'terraform'}", "output", "-json"], text=True)
    return {name: output["value"] for name, output in json.loads(raw).items()}


def invoke_deployed(outputs, prompt, customer_id, *, interaction_id=None):
    client = boto3.client("bedrock-agentcore", region_name=outputs["region"],
                          config=Config(connect_timeout=5, read_timeout=300,
                                        retries={"total_max_attempts": 1}))
    session_id = str(uuid4())
    interaction_id = interaction_id or str(uuid4())
    print(f"Interaction: {interaction_id}; runtime session: {session_id}", file=sys.stderr)
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=outputs["agent_runtime_arn"], runtimeSessionId=session_id,
            contentType="application/json", accept="application/json",
            payload=json.dumps({"prompt": prompt, "customer_id": customer_id,
                                "interaction_id": interaction_id}).encode(),
        )
        body = response["response"]
        try:
            result = json.loads(body.read())
        finally:
            body.close()
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"Agent returned a failure: {result}")
        return result
    finally:
        # Each script call is a standalone interaction; release its test microVM.
        # If invocation timed out, do not interrupt tools that may still be running.
        if "result" in locals() and isinstance(result, dict):
            try:
                client.stop_runtime_session(agentRuntimeArn=outputs["agent_runtime_arn"],
                                             runtimeSessionId=session_id)
            except Exception as exc:
                print(f"Session cleanup failed ({type(exc).__name__}); idle timeout will apply.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--customer-id", default="customer-alex",
                        help="Trusted operator's selected test customer, not an end-user login")
    args = parser.parse_args()
    try:
        print(json.dumps(invoke_deployed(deployment_outputs(), args.prompt, args.customer_id), indent=2))
    except Exception as exc:
        print(f"Invocation failed ({type(exc).__name__}). Inspect interaction records before retrying writes.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
