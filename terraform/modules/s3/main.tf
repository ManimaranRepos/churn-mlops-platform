# =============================================================================
# S3 MODULE — Data Lake Buckets
# =============================================================================
# Four-bucket data lake pattern:
#   raw         → events land here first, untouched
#   processed   → curated Parquet/Iceberg tables after Glue ETL
#   artifacts   → ML model files, MLflow experiment data
#   logs        → access logs from the other three (for audit compliance)
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  name_prefix = "${var.project}-${var.environment}"

  # All bucket names must be globally unique across all AWS accounts.
  # Including account_id guarantees uniqueness without needing random suffixes.
  buckets = {
    raw       = "${local.name_prefix}-raw-data-${var.account_id}"
    processed = "${local.name_prefix}-processed-data-${var.account_id}"
    artifacts = "${local.name_prefix}-model-artifacts-${var.account_id}"
    logs      = "${local.name_prefix}-access-logs-${var.account_id}"
  }
}

# -----------------------------------------------------------------------------
# Logs Bucket — Created first because other buckets reference it for logging
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "logs" {
  bucket = local.buckets.logs

  tags = {
    Name    = local.buckets.logs
    Purpose = "access-logs"
  }
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true # Reduces KMS API call costs by 99%
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expire-old-logs"
    status = "Enabled"
    filter { prefix = "" }

    expiration {
      days = var.log_retention_days
    }
  }
}

# The logs bucket itself needs an ACL that allows the S3 logging service to write
resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_acl" "logs" {
  depends_on = [aws_s3_bucket_ownership_controls.logs]
  bucket     = aws_s3_bucket.logs.id
  acl        = "log-delivery-write"
}

# -----------------------------------------------------------------------------
# Reusable module-level resource: we use a factory pattern with locals
# to avoid repeating the same 5 resource blocks 4 times.
# For the 3 main buckets, we use a local map and for_each.
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "data_lake" {
  for_each = {
    raw       = local.buckets.raw
    processed = local.buckets.processed
    artifacts = local.buckets.artifacts
  }

  bucket = each.value

  tags = {
    Name    = each.value
    Purpose = each.key
  }
}

resource "aws_s3_bucket_versioning" "data_lake" {
  for_each = aws_s3_bucket.data_lake
  bucket   = each.value.id

  versioning_configuration {
    # Versioning on raw/processed means we can recover accidentally overwritten data.
    # For model artifacts, versions let us roll back to a previous model binary.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  for_each = aws_s3_bucket.data_lake
  bucket   = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    # bucket_key_enabled: instead of calling KMS for every object, S3 uses
    # a per-bucket data key. Reduces KMS costs by up to 99% on high-volume buckets.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data_lake" {
  for_each = aws_s3_bucket.data_lake
  bucket   = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Log access to each data lake bucket → the logs bucket
resource "aws_s3_bucket_logging" "data_lake" {
  for_each = aws_s3_bucket.data_lake
  bucket   = each.value.id

  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "${each.key}/"
}

# -----------------------------------------------------------------------------
# Lifecycle Policies — Automatic cost optimization
# Data moves: S3 Standard → S3-IA (infrequent access) → Glacier → Delete
# This is crucial for a data lake that accumulates GBs of events daily
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.data_lake["raw"].id

  rule {
    id     = "raw-data-tiering"
    status = "Enabled"
    filter { prefix = "" }

    # After 90 days, events are rarely queried — move to cheaper storage
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    # After 365 days, archive to Glacier (retrieval takes minutes but costs pennies)
    transition {
      days          = 365
      storage_class = "GLACIER"
    }

    # Retain raw data for 7 years (common fintech compliance requirement)
    expiration {
      days = 2555
    }

    # Also clean up incomplete multipart uploads (can accumulate and cost money)
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    # Expire old versions after 90 days to avoid paying for version history forever
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "processed" {
  bucket = aws_s3_bucket.data_lake["processed"].id

  rule {
    id     = "processed-data-tiering"
    status = "Enabled"
    filter { prefix = "" }

    transition {
      days          = 180
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = var.processed_data_retention_days
      storage_class = "GLACIER"
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.data_lake["artifacts"].id

  rule {
    id     = "model-artifact-tiering"
    status = "Enabled"
    filter { prefix = "" }

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = var.model_artifacts_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# -----------------------------------------------------------------------------
# Bucket Policies — Enforce secure transport (HTTPS only)
# Reject any request that doesn't use TLS. Without this, misconfigured
# clients could send data in plaintext.
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "enforce_tls" {
  for_each = merge(aws_s3_bucket.data_lake, { logs = aws_s3_bucket.logs })

  statement {
    sid     = "DenyNonTLS"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      each.value.arn,
      "${each.value.arn}/*"
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "enforce_tls" {
  for_each = merge(aws_s3_bucket.data_lake, { logs = aws_s3_bucket.logs })
  bucket   = each.value.id
  policy   = data.aws_iam_policy_document.enforce_tls[each.key].json
}
