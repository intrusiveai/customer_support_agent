mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/support-test" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::support-test" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-west-2:123456789012:table/support-test" }
  }
  mock_resource "aws_s3vectors_index" {
    defaults = { index_arn = "arn:aws:s3vectors:us-west-2:123456789012:bucket/support-test/index/support-docs" }
  }
  mock_resource "aws_bedrockagent_knowledge_base" {
    defaults = { id = "TESTKB1234", arn = "arn:aws:bedrock:us-west-2:123456789012:knowledge-base/TESTKB1234" }
  }
  mock_resource "aws_bedrockagent_data_source" {
    defaults = { data_source_id = "TESTDS1234" }
  }
  mock_resource "aws_bedrockagentcore_agent_runtime" {
    defaults = {
      agent_runtime_id  = "support_agent_test-Abcdef1234"
      agent_runtime_arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/support_agent_test-Abcdef1234"
    }
  }
}

run "test_stack" {
  # Plan-only also prevents local provisioners from running (provider mocks alone do not).
  command = plan
  variables {
    # Only a plan fixture for file hashing; never submitted to an AWS runtime.
    deployment_zip = "../pyproject.toml"
  }
  assert {
    condition     = aws_dynamodb_table.support.billing_mode == "PAY_PER_REQUEST" && aws_dynamodb_table.support.hash_key == "pk" && aws_dynamodb_table.support.range_key == "sk"
    error_message = "The test stack must use a single on-demand pk/sk table."
  }
  assert {
    condition     = length(aws_dynamodb_table_item.customer) == 2 && length(aws_dynamodb_table_item.order) == 2
    error_message = "The two sample customers and orders must be seeded."
  }
  assert {
    condition     = aws_bedrockagentcore_agent_runtime.support.agent_runtime_artifact[0].code_configuration[0].runtime == "PYTHON_3_13"
    error_message = "Runtime must match the packaged Python 3.13 dependencies."
  }
  assert {
    condition     = output.runtime_config.dynamodb_table == aws_dynamodb_table.support.name
    error_message = "Agent must use the deployed DynamoDB table, not ephemeral SQLite."
  }
  assert {
    condition     = aws_s3vectors_index.support.dimension == 1024 && aws_bedrockagent_knowledge_base.support.storage_configuration[0].type == "S3_VECTORS"
    error_message = "Knowledge-base storage must match the Titan v2 vector dimensions."
  }
  assert {
    condition     = alltrue([for block in aws_s3_bucket_public_access_block.test : block.block_public_policy && block.block_public_acls])
    error_message = "Test S3 buckets must not be public."
  }
  assert {
    condition     = jsondecode(aws_iam_role.runtime.assume_role_policy).Statement[0].Principal.Service == "bedrock-agentcore.amazonaws.com"
    error_message = "Only the AgentCore service should assume the runtime role."
  }
}
