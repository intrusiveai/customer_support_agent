locals {
  foundation_model_id = replace(var.model_id, "/^(us|eu|apac|global)\\./", "")
  model_resources = startswith(var.model_id, "arn:") ? [var.model_id] : [
    "arn:${local.partition}:bedrock:*::foundation-model/${local.foundation_model_id}",
    "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.model_id}",
  ]
  runtime_config = {
    model_id              = var.model_id
    bedrock_region        = var.region
    knowledge_base_id     = aws_bedrockagent_knowledge_base.support.id
    knowledge_base_region = var.region
    s3_region             = var.region
    summary_bucket        = aws_s3_bucket.summaries.id
    summary_read_prefix   = "support/{customer_id}/{interaction_id}/"
    summary_write_prefix  = "support/{customer_id}/{interaction_id}/"
    dynamodb_table        = aws_dynamodb_table.support.name
    dynamodb_region       = var.region
  }
}

resource "aws_iam_role" "runtime" {
  name = "${var.name}-runtime"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "runtime" {
  name = "support-agent-access"
  role = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = concat(local.model_resources, var.additional_model_resource_arns)
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:Retrieve"]
        Resource = aws_bedrockagent_knowledge_base.support.arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.support.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.summaries.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.summaries.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.assets.arn}/code/*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:CreateLogStream", "logs:PutLogEvents", "logs:PutResourcePolicy"]
        Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/${local.runtime_name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:*"
      }
    ]
  })
}

resource "aws_bedrockagentcore_agent_runtime" "support" {
  agent_runtime_name = local.runtime_name
  description        = "Customer-support test agent with simulated refunds"
  role_arn           = aws_iam_role.runtime.arn
  agent_runtime_artifact {
    code_configuration {
      runtime     = "PYTHON_3_13"
      entry_point = ["main.py"]
      code {
        s3 {
          bucket = aws_s3_bucket.assets.id
          prefix = aws_s3_object.code.key
        }
      }
    }
  }
  network_configuration {
    network_mode = "PUBLIC"
  }
  protocol_configuration {
    server_protocol = "HTTP"
  }
  lifecycle_configuration {
    idle_runtime_session_timeout = 300
    max_lifetime                 = 1800
  }
  environment_variables = {
    SUPPORT_AGENT_CONFIG_JSON = jsonencode(local.runtime_config)
  }
  depends_on = [
    aws_iam_role_policy.runtime,
    aws_dynamodb_table_item.customer,
    aws_dynamodb_table_item.order,
    terraform_data.ingest,
  ]
}

resource "aws_cloudwatch_log_group" "runtime" {
  name              = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.support.agent_runtime_id}-DEFAULT"
  retention_in_days = 7
}
