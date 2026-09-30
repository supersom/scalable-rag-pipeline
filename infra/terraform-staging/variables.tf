# infra/terraform-staging/variables.tf

variable "aws_region" {
  description = "AWS region — must match the existing cluster's region."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "Name of the existing EKS cluster to attach staging resources to."
  type        = string
  default     = "rag-platform-cluster"
}

variable "vpc_cidr" {
  description = "CIDR of the existing VPC — used only for the staging RDS security group ingress rule."
  type        = string
  default     = "10.0.0.0/16"
}

variable "staging_evidence_bucket_mode" {
  description = "Pinned bucket choice: existing reuses retained storage, create makes a protected bucket and key. Use scripts/apply_staging_terraform.sh to select it."
  type        = string

  validation {
    condition     = contains(["existing", "create"], var.staging_evidence_bucket_mode)
    error_message = "staging_evidence_bucket_mode must be existing or create."
  }
}

variable "staging_evidence_existing_kms_key_arn" {
  description = "KMS key configured on a retained staging evidence bucket; only used in existing mode."
  type        = string
  default     = ""
}
