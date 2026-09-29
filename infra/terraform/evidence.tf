# infra/terraform/evidence.tf
#
# PR 5a: metadata-only evidence delivery infrastructure. No new database — the outbox tables
# live in the existing chat-history PostgreSQL instance (var.db_tier). This bucket and KMS key
# back only the drain consumer's queue-archive/ objects while EVAL_CAPTURE_CONTENT stays
# disabled; PR 5b would be the first thing to also write real payload content here.

resource "aws_kms_key" "evidence" {
  description             = "Evidence delivery archive encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "AllowEvidenceIRSARolesToUseKey"
        Effect = "Allow"
        Principal = {
          AWS = [
            module.evidence_worker_irsa_role.iam_role_arn,
            module.evidence_drain_consumer_irsa_role.iam_role_arn,
          ]
        }
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "evidence" {
  name          = "alias/${var.cluster_name}-evidence"
  target_key_id = aws_kms_key.evidence.key_id
}

resource "aws_s3_bucket" "evidence" {
  bucket = "${var.cluster_name}-evidence-prod-7649"

  tags = {
    Name = "Evidence delivery archive"
  }
}

resource "aws_s3_bucket_versioning" "evidence_ver" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence_sse" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.evidence.arn
    }
    bucket_key_enabled = true
  }
}

# Denies any write that doesn't request this key's SSE-KMS, enforcing server-side what the
# application already requests client-side.
data "aws_iam_policy_document" "evidence_bucket" {
  statement {
    sid       = "DenyNonKmsWrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.evidence.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = data.aws_iam_policy_document.evidence_bucket.json
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence_lifecycle" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "expire-archive-content"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_sqs_queue" "evidence_dlq" {
  name                      = "${var.cluster_name}-evidence-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "evidence" {
  name                       = "${var.cluster_name}-evidence"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.evidence_dlq.arn
    maxReceiveCount     = 5
  })
}

# --- Delivery worker: publishes committed outbox rows --------------------------------------

resource "aws_iam_policy" "evidence_worker" {
  name        = "RAG_Evidence_Worker_Policy"
  description = "Allows the evidence delivery worker to publish committed envelopes"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["sqs:SendMessage"]
        Effect   = "Allow"
        Resource = aws_sqs_queue.evidence.arn
      }
    ]
  })
}

module "evidence_worker_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-evidence-worker-role"

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["default:evidence-worker"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.evidence_worker.arn
  }
}

# --- Drain consumer: temporary consumer until Phase 3's evaluation worker exists -----------

resource "aws_iam_policy" "evidence_drain_consumer" {
  name        = "RAG_Evidence_Drain_Consumer_Policy"
  description = "Allows the drain consumer to read the evidence queue and archive envelopes to S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ]
        Effect   = "Allow"
        Resource = aws_sqs_queue.evidence.arn
      },
      {
        Action = ["s3:PutObject", "s3:GetObject"]
        Effect = "Allow"
        Resource = [
          "${aws_s3_bucket.evidence.arn}/queue-archive/*",
          "${aws_s3_bucket.evidence.arn}/queue-archive-conflict/*"
        ]
      },
      {
        Action   = ["s3:ListBucket"]
        Effect   = "Allow"
        Resource = aws_s3_bucket.evidence.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["queue-archive/*"]
          }
        }
      },
      {
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Effect   = "Allow"
        Resource = aws_kms_key.evidence.arn
      }
    ]
  })
}

module "evidence_drain_consumer_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-evidence-drain-consumer-role"

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["default:evidence-drain-consumer"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.evidence_drain_consumer.arn
  }
}
