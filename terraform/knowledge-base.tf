resource "aws_s3vectors_vector_bucket" "support" {
  vector_bucket_name = "${local.bucket_base}-vectors"
  force_destroy      = var.force_destroy
}

resource "aws_s3vectors_index" "support" {
  vector_bucket_name = aws_s3vectors_vector_bucket.support.vector_bucket_name
  index_name         = "support-docs"
  data_type          = "float32"
  dimension          = 1024
  distance_metric    = "cosine"
  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

resource "aws_iam_role" "knowledge_base" {
  name = "${var.name}-knowledge-base"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:knowledge-base/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "knowledge_base" {
  name = "knowledge-base-access"
  role = aws_iam_role.knowledge_base.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:${local.partition}:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.assets.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.assets.arn}/knowledge/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors", "s3vectors:QueryVectors", "s3vectors:GetIndex"]
        Resource = aws_s3vectors_index.support.index_arn
      }
    ]
  })
}

resource "aws_bedrockagent_knowledge_base" "support" {
  name     = var.name
  role_arn = aws_iam_role.knowledge_base.arn
  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:${local.partition}:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = 1024
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }
  storage_configuration {
    type = "S3_VECTORS"
    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.support.index_arn
    }
  }
  depends_on = [aws_iam_role_policy.knowledge_base]
}

resource "aws_bedrockagent_data_source" "support" {
  name                 = "sample-support-docs"
  knowledge_base_id    = aws_bedrockagent_knowledge_base.support.id
  data_deletion_policy = "DELETE"
  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = aws_s3_bucket.assets.arn
      inclusion_prefixes = ["knowledge/"]
    }
  }
  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "FIXED_SIZE"
      fixed_size_chunking_configuration {
        max_tokens         = 300
        overlap_percentage = 10
      }
    }
  }
}

# The provider has no ingestion-job resource. This finite local helper starts
# ingestion and waits for COMPLETE, and runs again when source content changes.
resource "terraform_data" "ingest" {
  triggers_replace = [
    aws_bedrockagent_knowledge_base.support.id,
    aws_bedrockagent_data_source.support.data_source_id,
    aws_s3_object.policy.source_hash,
    sha256(aws_s3_object.policy_metadata.content),
    filesha256("${path.module}/../scripts/ingest_knowledge_base.py"),
  ]
  provisioner "local-exec" {
    working_dir = "${path.module}/.."
    command     = "uv run --frozen python scripts/ingest_knowledge_base.py"
    environment = {
      SUPPORT_AWS_REGION     = var.region
      SUPPORT_KB_ID          = aws_bedrockagent_knowledge_base.support.id
      SUPPORT_DATA_SOURCE_ID = aws_bedrockagent_data_source.support.data_source_id
    }
  }
  depends_on = [aws_s3_object.policy, aws_s3_object.policy_metadata]
}
