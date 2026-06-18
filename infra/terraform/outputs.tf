# infra/terraform/outputs.tf

output "eks_cluster_name" {
  description = "The name of the EKS cluster."
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  description = "The endpoint for the EKS cluster's API server."
  value       = module.eks.cluster_endpoint
}

output "db_endpoint" {
  description = "Writer endpoint for the active database tier (Aurora or RDS, set by var.db_tier)."
  value       = local.db_endpoint
}

output "redis_primary_endpoint" {
  description = "The primary endpoint for the ElastiCache Redis cluster."
  value       = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "s3_documents_bucket_name" {
  description = "The name of the S3 bucket for document storage."
  value       = aws_s3_bucket.documents.id
}

output "karpenter_controller_role_arn" {
  description = "IAM role ARN for the Karpenter controller service account."
  value       = aws_iam_role.karpenter_controller.arn
}

output "karpenter_node_role_name" {
  description = "IAM role name used by Karpenter-launched EC2 nodes."
  value       = aws_iam_role.karpenter_node.name
}

output "s3_models_bucket_name" {
  description = "S3 bucket for HuggingFace model weights cache (used by GPU node cold starts)."
  value       = aws_s3_bucket.models.id
}

output "db_secret_arn" {
  description = "ARN of the DB-managed Secrets Manager secret holding the master password (Aurora or RDS)."
  value       = local.db_secret_arn
}
