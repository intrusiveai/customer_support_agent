output "agent_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.support.agent_runtime_arn
}

output "region" {
  value = var.region
}

output "dynamodb_table" {
  value = aws_dynamodb_table.support.name
}

output "summary_bucket" {
  value = aws_s3_bucket.summaries.id
}

output "knowledge_base_id" {
  value = aws_bedrockagent_knowledge_base.support.id
}

output "data_source_id" {
  value = aws_bedrockagent_data_source.support.data_source_id
}

output "runtime_config" {
  description = "Configuration also usable locally to inspect the cloud-backed agent. Contains no credentials."
  value       = local.runtime_config
}

output "invoker_policy" {
  description = "Attach to a trusted test operator/backend role if it does not already have invocation access."
  value = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"]
      Resource = [
        aws_bedrockagentcore_agent_runtime.support.agent_runtime_arn,
        "${aws_bedrockagentcore_agent_runtime.support.agent_runtime_arn}/runtime-endpoint/*",
      ]
    }]
  })
}
