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

output "ingestion_queue_url" {
  description = "SQS queue consumed by the document ingestion worker."
  value       = aws_sqs_queue.ingestion.id
}

output "ingestion_irsa_role_arn" {
  description = "IAM role used by the ingestion queue consumer and Ray workers."
  value       = module.ingestion_irsa_role.iam_role_arn
}

output "ray_models_irsa_role_arn" {
  description = "Read-only model-cache role used by Ray Serve GPU workers."
  value       = module.ray_models_irsa_role.iam_role_arn
}

output "tavily_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Tavily API key. Empty string if var.tavily_api_key was not set (feature intentionally unconfigured)."
  value       = try(aws_secretsmanager_secret.tavily_api_key[0].arn, "")
}

output "ui_ecr_repository_url" {
  description = "ECR repository URL for the Streamlit UI image."
  value       = aws_ecr_repository.ui.repository_url
}

output "evidence_queue_url" {
  description = "SQS queue for committed evidence envelopes."
  value       = aws_sqs_queue.evidence.id
}

output "evidence_bucket_name" {
  description = "S3 bucket for the drain consumer's queue-archive objects (and future PR 5b payload content)."
  value       = aws_s3_bucket.evidence.id
}

output "evidence_kms_key_arn" {
  description = "KMS key ARN required on every write to the evidence bucket."
  value       = aws_kms_key.evidence.arn
}

output "evidence_worker_irsa_role_arn" {
  description = "IAM role used by the evidence delivery worker."
  value       = module.evidence_worker_irsa_role.iam_role_arn
}

output "evidence_drain_consumer_irsa_role_arn" {
  description = "IAM role used by the evidence drain consumer."
  value       = module.evidence_drain_consumer_irsa_role.iam_role_arn
}
