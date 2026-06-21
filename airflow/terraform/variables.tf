variable "project"     { default = "churn-platform" }
variable "environment" { default = "dev" }

variable "eks_oidc_provider_arn" { description = "EKS OIDC provider ARN for IRSA" }
variable "eks_oidc_provider_url" { description = "EKS OIDC provider URL (https://...)" }

variable "aurora_host"                  { description = "Aurora cluster endpoint" }
variable "sagemaker_execution_role_arn" { description = "SageMaker training execution role ARN" }

variable "raw_bucket"       { description = "S3 bucket for raw data" }
variable "processed_bucket" { description = "S3 bucket for processed/feature data" }
variable "artifacts_bucket" { description = "S3 bucket for artifacts + logs" }
variable "kinesis_stream_name" { description = "Kinesis data stream name" }

variable "kms_key_arn_secrets"    { description = "KMS CMK for Secrets Manager" }
variable "kms_key_arn_s3"         { description = "KMS CMK for S3" }
variable "kms_key_arn_cloudwatch" { description = "KMS CMK for CloudWatch" }

variable "log_retention_days" { default = 14 }
