"""ZIP entrypoint; run with the AgentCore Python 3.13 managed runtime."""

import asyncio

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from customer_support.runtime import handle_request

app = BedrockAgentCoreApp()


@app.entrypoint
async def support(payload):
    # The synchronous Strands invocation runs off the HTTP event loop so /ping stays responsive.
    return await asyncio.to_thread(handle_request, payload)


if __name__ == "__main__":
    app.run()
