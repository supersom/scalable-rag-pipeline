# infra/terraform-staging/evidence.tf
#
# Staging mirror of infra/terraform/evidence.tf (production) — its own KMS/S3/SQS/IAM,
# scoped to the "staging-evidence" namespace, disposable (force_destroy, short KMS deletion
# window). References data.aws_iam_openid_connect_provider.existing instead of module.eks.

resource "aws_kms_key" "staging_evidence" {
  description             = "Staging evidence delivery archive encryption"
  deletion_window_in_days = 7 # shorter than prod — staging is disposable
  enable_key_rotation     = true
}

resource "aws_s3_bucket" "staging_evidence" {
  bucket        = "${var.cluster_name}-evidence-staging-7649"
  force_destroy = true # disposable — safe to tear down without manual emptying

  tags = {
    Name = "Staging evidence delivery archive"
  }
}

resource "aws_s3_bucket_versioning" "staging_evidence_ver" {
  bucket = aws_s3_bucket.staging_evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "staging_evidence_sse" {
  bucket = aws_s3_bucket.staging_evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.staging_evidence.arn
    }
    bucket_key_enabled = true
  }
}

# Denies any write that doesn't request this key's SSE-KMS, enforcing server-side what the
# application already requests client-side. StringNotEquals (not StringNotEqualsIfExists) is
# deliberate — a missing SSE-KMS header must trigger the Deny, not be silently exempted.
data "aws_iam_policy_document" "staging_evidence_bucket" {
  statement {
    sid       = "DenyNonKmsWrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.staging_evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.staging_evidence.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "staging_evidence" {
  bucket = aws_s3_bucket.staging_evidence.id
  policy = data.aws_iam_policy_document.staging_evidence_bucket.json
}

resource "aws_s3_bucket_lifecycle_configuration" "staging_evidence_lifecycle" {
  bucket = aws_s3_bucket.staging_evidence.id

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

resource "aws_sqs_queue" "staging_evidence_dlq" {
  name                      = "${var.cluster_name}-staging-evidence-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "staging_evidence" {
  name                       = "${var.cluster_name}-staging-evidence"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.staging_evidence_dlq.arn
    maxReceiveCount     = 5
  })
}

# --- Delivery worker: publishes committed outbox rows --------------------------------------

resource "aws_iam_policy" "staging_evidence_worker" {
  name        = "RAG_Staging_Evidence_Worker_Policy"
  description = "Allows the staging evidence delivery worker to publish committed envelopes"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["sqs:SendMessage"]
        Effect   = "Allow"
        Resource = aws_sqs_queue.staging_evidence.arn
      }
    ]
  })
}

module "staging_evidence_worker_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-staging-evidence-worker-role"

  oidc_providers = {
    main = {
      provider_arn               = data.aws_iam_openid_connect_provider.existing.arn
      namespace_service_accounts = ["staging-evidence:evidence-worker"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.staging_evidence_worker.arn
  }
}

# --- Drain consumer: temporary consumer until Phase 3's evaluation worker exists -----------

resource "aws_iam_policy" "staging_evidence_drain_consumer" {
  name        = "RAG_Staging_Evidence_Drain_Consumer_Policy"
  description = "Allows the staging drain consumer to read the evidence queue and archive envelopes to S3"

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
        Resource = aws_sqs_queue.staging_evidence.arn
      },
      {
        Action = ["s3:PutObject", "s3:GetObject"]
        Effect = "Allow"
        Resource = [
          "${aws_s3_bucket.staging_evidence.arn}/queue-archive/*",
          "${aws_s3_bucket.staging_evidence.arn}/queue-archive-conflict/*"
        ]
      },
      {
        Action   = ["s3:ListBucket"]
        Effect   = "Allow"
        Resource = aws_s3_bucket.staging_evidence.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["queue-archive/*"]
          }
        }
      },
      {
        Action   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"]
        Effect   = "Allow"
        Resource = aws_kms_key.staging_evidence.arn
      }
    ]
  })
}

module "staging_evidence_drain_consumer_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-staging-evidence-drain-consumer-role"

  oidc_providers = {
    main = {
      provider_arn               = data.aws_iam_openid_connect_provider.existing.arn
      namespace_service_accounts = ["staging-evidence:evidence-drain-consumer"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.staging_evidence_drain_consumer.arn
  }
}
