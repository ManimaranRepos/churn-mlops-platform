# =============================================================================
# BOOTSTRAP — Terraform Remote State Infrastructure
# =============================================================================
# Run this ONCE before any other Terraform in this repo.
# It creates the S3 bucket and DynamoDB table that all other modules use
# as their backend. After applying, copy the backend config into each
# environment's backend.tf.
#
# Usage:
#   cd terraform/bootstrap
#   terraform init    # uses local state (intentional — this is the bootstrap)
#   terraform apply
# =============================================================================

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # Bootstrap intentionally uses LOCAL state — it is the thing that creates
  # the remote state bucket, so it cannot use remote state itself.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "churn-platform"
      ManagedBy   = "terraform"
      Team        = "mlops"
      CostCenter  = "ai-platform"
      Environment = "global"
    }
  }
}

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name prefix used in all resource names"
  type        = string
  default     = "churn-platform"
}

# -----------------------------------------------------------------------------
# S3 Bucket — Terraform State Storage
# -----------------------------------------------------------------------------
# Why versioning? If a terraform apply corrupts state (rare but possible),
# we can roll back to the previous state version.
# Why server-side encryption? State files can contain sensitive values
# (DB passwords, etc.) even though we try to avoid it.
resource "aws_s3_bucket" "terraform_state" {
  bucket = "${var.project}-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Prevent accidental deletion of this bucket — losing it means losing
  # track of all managed infrastructure
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block ALL public access — state files must never be public
resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket                  = aws_s3_bucket.terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# DynamoDB Table — State Locking
# -----------------------------------------------------------------------------
# Why PAY_PER_REQUEST? This table gets maybe 1 write per deploy.
# Provisioned capacity would waste money on a near-idle table.
resource "aws_dynamodb_table" "terraform_locks" {
  name         = "${var.project}-terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

# Used to get the current AWS account ID for globally unique bucket naming
data "aws_caller_identity" "current" {}

# -----------------------------------------------------------------------------
# Outputs — Copy these into each environment's backend.tf
# -----------------------------------------------------------------------------
output "state_bucket_name" {
  value       = aws_s3_bucket.terraform_state.bucket
  description = "Use this as the bucket in each environment's backend config"
}

output "dynamodb_table_name" {
  value       = aws_dynamodb_table.terraform_locks.name
  description = "Use this as the dynamodb_table in each environment's backend config"
}

output "backend_config_snippet" {
  value = <<-EOT
    # Paste this into each environment's backend.tf:
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.terraform_state.bucket}"
        key            = "<environment>/terraform.tfstate"
        region         = "${var.aws_region}"
        dynamodb_table = "${aws_dynamodb_table.terraform_locks.name}"
        encrypt        = true
      }
    }
  EOT
}
