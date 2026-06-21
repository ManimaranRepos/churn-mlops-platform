# =============================================================================
# DEV ENVIRONMENT — Root Module
# =============================================================================
# This file wires together all Phase 1 modules for the dev environment.
# Module call order matters: KMS → Security → S3 → VPC → IAM
# (later modules reference outputs from earlier ones)
# =============================================================================

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  # All resources get these tags automatically — no need to repeat in every module
  default_tags {
    tags = {
      Environment = var.environment
      Project     = var.project
      ManagedBy   = "terraform"
      Team        = "mlops"
      CostCenter  = "ai-platform"
    }
  }
}

# Fetch current account ID and region for use in resource naming
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# MODULE 1: KMS — Created first because all other modules need encryption keys
# =============================================================================
module "kms" {
  source = "../../modules/kms"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region
  account_id  = data.aws_caller_identity.current.account_id

  # Dev: 7-day deletion window (prod should be 30 for safety)
  key_deletion_window_days = 7
  enable_key_rotation      = true
}

# =============================================================================
# MODULE 2: SECURITY — CloudTrail, Secrets, Log Groups
# Needs KMS keys, needs the logs bucket (chicken-and-egg solved by creating
# the logs bucket inline here with a minimal config before the S3 module)
# =============================================================================

# We need the logs bucket ARN for CloudTrail before the full S3 module runs.
# The S3 module creates it fully; we just need to know the name here.
locals {
  logs_bucket_name = "${var.project}-${var.environment}-access-logs-${data.aws_caller_identity.current.account_id}"
}

module "security" {
  source = "../../modules/security"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region
  account_id  = data.aws_caller_identity.current.account_id

  logs_bucket_name       = local.logs_bucket_name
  logs_bucket_arn        = "arn:aws:s3:::${local.logs_bucket_name}"
  cloudwatch_kms_key_arn = module.kms.cloudwatch_key_arn
  secrets_kms_key_arn    = module.kms.secrets_key_arn

  log_retention_days = 14 # Short in dev — data isn't valuable here
}

# =============================================================================
# MODULE 3: S3 — Data Lake Buckets
# Needs KMS key for encryption
# =============================================================================
module "s3" {
  source = "../../modules/s3"

  project     = var.project
  environment = var.environment
  account_id  = data.aws_caller_identity.current.account_id

  kms_key_arn = module.kms.s3_key_arn

  # Shorter retention in dev — we're not storing real customer data
  raw_data_retention_days        = 30
  processed_data_retention_days  = 30
  model_artifacts_retention_days = 30
  log_retention_days             = 14
}

# =============================================================================
# MODULE 4: VPC — Network Foundation
# Needs CloudWatch KMS key for Flow Logs
# Dev uses single_nat_gateway to save ~$100/month
# =============================================================================
module "vpc" {
  source = "../../modules/vpc"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  vpc_cidr = "10.0.0.0/16"
  availability_zones = [
    "${var.aws_region}a",
    "${var.aws_region}b",
    "${var.aws_region}c"
  ]

  # Dev cost optimisation: single NAT gateway instead of one per AZ
  # Trade-off: if the AZ hosting the NAT fails, ALL private subnets lose internet
  # This is acceptable in dev — never acceptable in prod
  enable_nat_gateway = true
  single_nat_gateway = true

  cloudwatch_kms_key_arn   = module.kms.cloudwatch_key_arn
  flow_logs_retention_days = 7 # Minimal in dev
}

# =============================================================================
# MODULE 5: IAM — Roles and Policies
# Needs S3 bucket ARNs and KMS key ARNs to scope permissions correctly
# =============================================================================
module "iam" {
  source = "../../modules/iam"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region
  account_id  = data.aws_caller_identity.current.account_id

  raw_bucket_arn       = module.s3.raw_bucket_arn
  processed_bucket_arn = module.s3.processed_bucket_arn
  artifacts_bucket_arn = module.s3.artifacts_bucket_arn
  logs_bucket_arn      = module.s3.logs_bucket_arn

  kms_s3_key_arn      = module.kms.s3_key_arn
  kms_secrets_key_arn = module.kms.secrets_key_arn
}

# =============================================================================
# OUTPUTS — Values needed by Phase 2 (EKS) and beyond
# These are also visible in the Terraform state and `terraform output` command
# =============================================================================
output "vpc_id" {
  value = module.vpc.vpc_id
}

output "private_subnet_ids" {
  value = module.vpc.private_subnet_ids
}

output "public_subnet_ids" {
  value = module.vpc.public_subnet_ids
}

output "database_subnet_ids" {
  value = module.vpc.database_subnet_ids
}

output "nat_gateway_ips" {
  value       = module.vpc.nat_gateway_ips
  description = "Whitelist these IPs in any external API you integrate with"
}

output "eks_node_role_arn" {
  value = module.iam.eks_node_role_arn
}

output "sagemaker_role_arn" {
  value = module.iam.sagemaker_role_arn
}

output "glue_role_arn" {
  value = module.iam.glue_role_arn
}

output "lambda_role_arn" {
  value = module.iam.lambda_role_arn
}

output "cicd_role_arn" {
  value       = module.iam.cicd_role_arn
  description = "Add to GitHub Actions workflow: role-to-assume"
}

output "raw_bucket_name" {
  value = module.s3.raw_bucket_name
}

output "processed_bucket_name" {
  value = module.s3.processed_bucket_name
}

output "artifacts_bucket_name" {
  value = module.s3.artifacts_bucket_arn
}

output "alerts_sns_topic_arn" {
  value = module.security.alerts_sns_topic_arn
}

output "kms_s3_key_arn" {
  value = module.kms.s3_key_arn
}

output "kms_eks_key_arn" {
  value = module.kms.eks_key_arn
}

output "kms_rds_key_arn" {
  value = module.kms.rds_key_arn
}
