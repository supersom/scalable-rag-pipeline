# infra/terraform-staging/main.tf
#
# Separate state from infra/terraform's production state — a mistake here cannot corrupt
# production. Reads the existing cluster/VPC by data lookup rather than duplicating them.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket       = "rag-platform-terraform-state-prod-7649"
    key          = "platform/staging-evidence.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "Enterprise-RAG"
      Environment = "staging-evidence"
      ManagedBy   = "Terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
