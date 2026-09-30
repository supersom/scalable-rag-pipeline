# infra/terraform-staging/evidence.tf
#
# The bucket mode is selected once by scripts/apply_staging_terraform.sh and
# saved as a Terraform output. A retained bucket stays outside this state;
# a newly created bucket and key stay managed by this state. Both paths use
# the same lifecycle, queue, and IAM configuration.

locals {
  create_staging_evidence_bucket = var.staging_evidence_bucket_mode == "create"
  staging_evidence_bucket_id     = local.create_staging_evidence_bucket ? aws_s3_bucket.staging_evidence[0].id : data.aws_s3_bucket.staging_evidence[0].id
  staging_evidence_bucket_arn    = local.create_staging_evidence_bucket ? aws_s3_bucket.staging_evidence[0].arn : data.aws_s3_bucket.staging_evidence[0].arn
  staging_evidence_kms_key_arn   = local.create_staging_evidence_bucket ? aws_kms_key.staging_evidence[0].arn : data.aws_kms_key.staging_evidence[0].arn
}

data "aws_kms_key" "staging_evidence" {
  count  = local.create_staging_evidence_bucket ? 0 : 1
  key_id = var.staging_evidence_existing_kms_key_arn

  lifecycle {
    precondition {
      condition     = var.staging_evidence_existing_kms_key_arn != ""
      error_message = "Existing bucket mode requires its SSE-KMS key ARN. Run bash scripts/apply_staging_terraform.sh apply instead of a direct terraform apply."
    }
  }
}

data "aws_s3_bucket" "staging_evidence" {
  count  = local.create_staging_evidence_bucket ? 0 : 1
  bucket = "${var.cluster_name}-evidence-staging-7649"
}

resource "aws_kms_key" "staging_evidence" {
  count                   = local.create_staging_evidence_bucket ? 1 : 0
  description             = "Staging evidence delivery archive encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  # The account principal enables IAM policies to delegate key use to the
  # drain consumer without creating a dependency cycle through its IAM role.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EnableAccountIAMPolicies"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = "kms:*"
      Resource  = "*"
    }]
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket" "staging_evidence" {
  count         = local.create_staging_evidence_bucket ? 1 : 0
  bucket        = "${var.cluster_name}-evidence-staging-7649"
  force_destroy = false

  tags = {
    Name = "Staging evidence delivery archive"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "staging_evidence" {
  count  = local.create_staging_evidence_bucket ? 1 : 0
  bucket = aws_s3_bucket.staging_evidence[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "staging_evidence" {
  count  = local.create_staging_evidence_bucket ? 1 : 0
  bucket = aws_s3_bucket.staging_evidence[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.staging_evidence[0].arn
    }
    bucket_key_enabled = true
  }
}

# A new bucket needs the same write restriction already present on the retained bucket.
data "aws_iam_policy_document" "staging_evidence_bucket" {
  count = local.create_staging_evidence_bucket ? 1 : 0

  statement {
    sid       = "DenyNonKmsWrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.staging_evidence[0].arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.staging_evidence[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "staging_evidence" {
  count  = local.create_staging_evidence_bucket ? 1 : 0
  bucket = aws_s3_bucket.staging_evidence[0].id
  policy = data.aws_iam_policy_document.staging_evidence_bucket[0].json
}

resource "aws_s3_bucket_lifecycle_configuration" "staging_evidence_lifecycle" {
  bucket = local.staging_evidence_bucket_id

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
          "${local.staging_evidence_bucket_arn}/queue-archive/*",
          "${local.staging_evidence_bucket_arn}/queue-archive-conflict/*"
        ]
      },
      {
        Action   = ["s3:ListBucket"]
        Effect   = "Allow"
        Resource = local.staging_evidence_bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["queue-archive/*"]
          }
        }
      },
      {
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Effect   = "Allow"
        Resource = local.staging_evidence_kms_key_arn
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
