# infra/terraform/variables.tf

variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-1" # N. Virginia has the best GPU availability
}

variable "environment" {
  description = "Environment name (e.g., dev, prod)"
  type        = string
  default     = "prod"
}

variable "cluster_name" {
  description = "Name of the EKS Cluster"
  type        = string
  default     = "rag-platform-cluster"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16" # Gives us 65,536 IPs
}

variable "tavily_api_key" {
  description = <<-EOT
    Tavily Search API key for the RAG agent's web_search tool. Optional —
    web_search degrades gracefully (returns a "disabled" message) if unset.
    Set via terraform.tfvars (gitignored), never passed on the command line
    or as a shell env var. When set, provisions a Secrets Manager secret
    whose ARN bootstrap_cluster.sh stage 11 fetches at deploy time.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "db_tier" {
  description = <<-EOT
    Database tier for chat history storage.
      "rds"    — db.t3.micro RDS PostgreSQL, ~$15/month, no HA. Use for dev/test.
      "aurora" — Serverless v2 Aurora PostgreSQL, ~$86/month minimum, multi-AZ HA. Use for production.
    Set in terraform.tfvars; defaults to "rds" so a fresh clone is cheap out of the box.
  EOT
  type        = string
  default     = "rds"

  validation {
    condition     = contains(["aurora", "rds"], var.db_tier)
    error_message = "db_tier must be \"aurora\" or \"rds\"."
  }
}