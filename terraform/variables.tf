variable "region" {
  description = "AWS region supporting AgentCore Runtime, S3 Vectors, and the selected Bedrock models."
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Short, unique test deployment name."
  type        = string
  default     = "support-agent-test"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name))
    error_message = "Use 3–25 lowercase letters, digits, or hyphens, starting with a letter (leaves room for S3 name suffixes)."
  }
}

variable "model_id" {
  description = "Tool-capable Bedrock model or inference-profile ID available to your account."
  type        = string
  default     = "us.amazon.nova-lite-v1:0"
}

variable "additional_model_resource_arns" {
  description = "Underlying model ARNs needed for a custom/application inference profile, if using one."
  type        = list(string)
  default     = []
}

variable "deployment_zip" {
  description = "Path to the ARM64 Python 3.13 ZIP produced by scripts/package_agentcore.py."
  type        = string
  default     = "../dist/agentcore.zip"
}

variable "force_destroy" {
  description = "Allow terraform destroy to remove nonempty test S3 buckets (including summaries and code)."
  type        = bool
  default     = false
}
