# =============================================================================
# KMS MODULE — Customer Managed Encryption Keys
# =============================================================================
# We create one CMK per service boundary. This follows the principle of
# least-privilege for encryption: if SageMaker is compromised, the attacker
# cannot decrypt S3 data lake contents because they are on a different key.
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

  # Reusable key policy — allows the account root full control and enables
  # IAM policies to delegate key usage. Without this, even account admins
  # can get locked out of their own keys.
  base_key_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootAccountAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.aws_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# S3 Data Lake Key
# Encrypts: raw-data bucket, processed bucket, model artifacts bucket
# -----------------------------------------------------------------------------
resource "aws_kms_key" "s3" {
  description             = "${local.name_prefix} S3 data lake encryption key"
  deletion_window_in_days = var.key_deletion_window_days
  enable_key_rotation     = var.enable_key_rotation
  policy                  = local.base_key_policy
}

resource "aws_kms_alias" "s3" {
  name          = "alias/${local.name_prefix}-s3"
  target_key_id = aws_kms_key.s3.key_id
}

# -----------------------------------------------------------------------------
# RDS Key
# Encrypts: Aurora PostgreSQL (MLflow backend, Airflow metadata DB)
# Separate from S3 — database encryption has different access patterns
# -----------------------------------------------------------------------------
resource "aws_kms_key" "rds" {
  description             = "${local.name_prefix} RDS encryption key"
  deletion_window_in_days = var.key_deletion_window_days
  enable_key_rotation     = var.enable_key_rotation
  policy                  = local.base_key_policy
}

resource "aws_kms_alias" "rds" {
  name          = "alias/${local.name_prefix}-rds"
  target_key_id = aws_kms_key.rds.key_id
}

# -----------------------------------------------------------------------------
# EKS / EBS Key
# Encrypts: EKS node EBS volumes, Kubernetes secrets at rest
# Why separate? EKS needs specific key grants for the EBS CSI driver
# -----------------------------------------------------------------------------
resource "aws_kms_key" "eks" {
  description             = "${local.name_prefix} EKS and EBS encryption key"
  deletion_window_in_days = var.key_deletion_window_days
  enable_key_rotation     = var.enable_key_rotation

  # EKS secrets encryption requires a slightly different policy
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootAccountAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "eks" {
  name          = "alias/${local.name_prefix}-eks"
  target_key_id = aws_kms_key.eks.key_id
}

# -----------------------------------------------------------------------------
# Secrets Manager Key
# Encrypts: database credentials, API keys, tokens stored in Secrets Manager
# -----------------------------------------------------------------------------
resource "aws_kms_key" "secrets" {
  description             = "${local.name_prefix} Secrets Manager encryption key"
  deletion_window_in_days = var.key_deletion_window_days
  enable_key_rotation     = var.enable_key_rotation

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootAccountAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowSecretsManagerService"
        Effect = "Allow"
        Principal = {
          Service = "secretsmanager.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${local.name_prefix}-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

# -----------------------------------------------------------------------------
# CloudWatch Logs Key
# Why separate? CloudWatch Logs needs specific grants to use CMKs,
# and we don't want log encryption tied to data encryption key lifecycle
# -----------------------------------------------------------------------------
resource "aws_kms_key" "cloudwatch" {
  description             = "${local.name_prefix} CloudWatch Logs encryption key"
  deletion_window_in_days = var.key_deletion_window_days
  enable_key_rotation     = var.enable_key_rotation
  policy                  = local.base_key_policy
}

resource "aws_kms_alias" "cloudwatch" {
  name          = "alias/${local.name_prefix}-cloudwatch"
  target_key_id = aws_kms_key.cloudwatch.key_id
}
