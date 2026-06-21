variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "account_id" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "raw_bucket_arn" {
  type        = string
  description = "ARN of the raw data S3 bucket"
}

variable "processed_bucket_arn" {
  type        = string
  description = "ARN of the processed data S3 bucket"
}

variable "artifacts_bucket_arn" {
  type        = string
  description = "ARN of the model artifacts S3 bucket"
}

variable "logs_bucket_arn" {
  type        = string
  description = "ARN of the access logs S3 bucket"
}

variable "kms_s3_key_arn" {
  type        = string
  description = "ARN of the KMS key used for S3 encryption"
}

variable "kms_secrets_key_arn" {
  type        = string
  description = "ARN of the KMS key used for Secrets Manager"
}
