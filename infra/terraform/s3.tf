# infra/terraform/s3.tf

resource "aws_s3_bucket" "documents" {
  bucket = "rag-platform-documents-prod-7649" # Must be globally unique

  force_destroy = true

  tags = {
    Name = "Documents Bucket"
  }
}

# Enable Versioning: If a user overwrites a file, we keep the old one
resource "aws_s3_bucket_versioning" "docs_ver" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ENABLE TRANSFER ACCELERATION (Critical for global users uploading 1GB files)
# Uses AWS Edge locations to route data faster to the bucket
resource "aws_s3_bucket_accelerate_configuration" "docs_accel" {
  bucket = aws_s3_bucket.documents.id
  status = "Enabled"
}

# Lifecycle Rule: Move old raw files to cheaper storage (Intelligent Tiering)
resource "aws_s3_bucket_lifecycle_configuration" "docs_lifecycle" {
  bucket = aws_s3_bucket.documents.id

  rule {
    id     = "archive-old-files"
    status = "Enabled"

    filter {
      prefix = ""
    }

    transition {
      days          = 30
      storage_class = "INTELLIGENT_TIERING" # Auto-optimizes cost
    }
  }
}

# CORS Rule: Allow Browser (Frontend) to upload directly to S3
resource "aws_s3_bucket_cors_configuration" "docs_cors" {
  bucket = aws_s3_bucket.documents.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["PUT", "POST", "GET"]
    allowed_origins = ["https://your-rag-domain.com"] # Restrict to your domain
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# Model weights cache bucket — holds HuggingFace model snapshots so GPU node cold
# starts pull from S3 (free via Gateway endpoint) instead of HuggingFace (NAT, ~$0.90/node).
resource "aws_s3_bucket" "models" {
  bucket        = "rag-platform-models-prod-7649"
  force_destroy = true # Weights are re-uploadable from HuggingFace; no data-loss risk

  tags = {
    Name = "Model weights cache"
  }
}

# No versioning — model weights are immutable snapshots keyed by model ID + revision.
resource "aws_s3_bucket_versioning" "models_ver" {
  bucket = aws_s3_bucket.models.id
  versioning_configuration {
    status = "Disabled"
  }
}