terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.70.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project = var.name
      Purpose = "customer-support-agent-test"
    }
  }
}
