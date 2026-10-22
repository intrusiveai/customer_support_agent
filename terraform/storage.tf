data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  partition    = data.aws_partition.current.partition
  runtime_name = replace(var.name, "-", "_")
  bucket_base  = "${var.name}-${local.account_id}-${var.region}"
  seed         = jsondecode(file("${path.module}/../examples/seed-data.json"))
}

# Code and KB source documents are separated from the intentionally model-writable summaries.
resource "aws_s3_bucket" "assets" {
  bucket        = "${local.bucket_base}-assets"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket" "summaries" {
  bucket        = "${local.bucket_base}-summaries"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "test" {
  for_each                = { assets = aws_s3_bucket.assets.id, summaries = aws_s3_bucket.summaries.id }
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "test" {
  for_each = { assets = aws_s3_bucket.assets.id, summaries = aws_s3_bucket.summaries.id }
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "code" {
  bucket       = aws_s3_bucket.assets.id
  key          = "code/${filesha256(var.deployment_zip)}.zip"
  source       = var.deployment_zip
  source_hash  = filesha256(var.deployment_zip)
  content_type = "application/zip"
}

resource "aws_s3_object" "policy" {
  bucket       = aws_s3_bucket.assets.id
  key          = "knowledge/support-policy.md"
  source       = "${path.module}/../examples/support-policy.md"
  source_hash  = filesha256("${path.module}/../examples/support-policy.md")
  content_type = "text/markdown"
}

resource "aws_s3_object" "policy_metadata" {
  bucket       = aws_s3_bucket.assets.id
  key          = "knowledge/support-policy.md.metadata.json"
  content      = jsonencode({ metadataAttributes = { title = "Fictional demo support policy" } })
  content_type = "application/json"
}

resource "aws_dynamodb_table" "support" {
  name         = var.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

resource "aws_dynamodb_table_item" "customer" {
  for_each   = { for customer in local.seed.customers : customer.customer_id => customer }
  table_name = aws_dynamodb_table.support.name
  hash_key   = aws_dynamodb_table.support.hash_key
  range_key  = aws_dynamodb_table.support.range_key
  item = jsonencode({
    pk   = { S = "CUSTOMER#${each.key}" }
    sk   = { S = "PROFILE" }
    data = { S = jsonencode(each.value) }
  })
  lifecycle {
    ignore_changes = [item]
  }
}

resource "aws_dynamodb_table_item" "order" {
  for_each   = { for order in local.seed.orders : order.order_id => order }
  table_name = aws_dynamodb_table.support.name
  hash_key   = aws_dynamodb_table.support.hash_key
  range_key  = aws_dynamodb_table.support.range_key
  item = jsonencode({
    pk             = { S = "ORDER#${each.key}" }
    sk             = { S = "ORDER" }
    data           = { S = jsonencode(each.value) }
    refunded_minor = { N = "0" }
  })
  # A later apply must not reset balances modified by real test invocations.
  lifecycle {
    ignore_changes = [item]
  }
}
