# infra/terraform-staging/outputs.tf

output "staging_db_endpoint" {
  description = "Writer endpoint for the staging evidence RDS instance."
  value       = aws_db_instance.staging_evidence_postgres.address
}

output "staging_db_secret_arn" {
  description = "ARN of the DB-managed Secrets Manager secret holding the staging master password."
  value       = aws_db_instance.staging_evidence_postgres.master_user_secret[0].secret_arn
}

output "staging_evidence_queue_url" {
  description = "SQS queue for committed staging evidence envelopes."
  value       = aws_sqs_queue.staging_evidence.id
}

output "staging_evidence_bucket_name" {
  description = "S3 bucket for the staging drain consumer's queue-archive objects."
  value       = aws_s3_bucket.staging_evidence.id
}

output "staging_evidence_kms_key_arn" {
  description = "KMS key ARN required on every write to the staging evidence bucket."
  value       = aws_kms_key.staging_evidence.arn
}

output "staging_evidence_worker_irsa_role_arn" {
  description = "IAM role used by the staging evidence delivery worker."
  value       = module.staging_evidence_worker_irsa_role.iam_role_arn
}

output "staging_evidence_drain_consumer_irsa_role_arn" {
  description = "IAM role used by the staging evidence drain consumer."
  value       = module.staging_evidence_drain_consumer_irsa_role.iam_role_arn
}
