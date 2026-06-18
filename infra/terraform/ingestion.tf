# Queue-backed document ingestion. S3 sends object-created events to SQS; a
# Kubernetes worker consumes the queue and submits Ray jobs inside EKS.

resource "aws_ecr_repository" "ingestion" {
  name                 = "services/ingestion"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_sqs_queue" "ingestion_dlq" {
  name                      = "${var.cluster_name}-ingestion-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "ingestion" {
  name                       = "${var.cluster_name}-ingestion"
  visibility_timeout_seconds = 900
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 345600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingestion_dlq.arn
    maxReceiveCount     = 3
  })
}

data "aws_iam_policy_document" "ingestion_queue" {
  statement {
    sid       = "AllowDocumentBucketNotifications"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingestion.arn]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.documents.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "ingestion" {
  queue_url = aws_sqs_queue.ingestion.id
  policy    = data.aws_iam_policy_document.ingestion_queue.json
}

resource "aws_s3_bucket_notification" "document_ingestion" {
  bucket = aws_s3_bucket.documents.id

  queue {
    id        = "document-created"
    queue_arn = aws_sqs_queue.ingestion.arn
    events    = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_sqs_queue_policy.ingestion]
}
