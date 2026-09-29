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
