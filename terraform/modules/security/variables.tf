variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "logs_bucket_name" {
  type        = string
  description = "S3 bucket name for CloudTrail logs"
}

variable "logs_bucket_arn" {
  type        = string
  description = "S3 bucket ARN for CloudTrail policy"
}

variable "cloudwatch_kms_key_arn" {
  type        = string
  description = "KMS key for CloudWatch log group encryption"
}

variable "secrets_kms_key_arn" {
  type        = string
  description = "KMS key for Secrets Manager encryption"
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "vpc_id" {
  type        = string
  description = "VPC ID for flow logs"
}

variable "raw_bucket" {
  type        = string
  description = "Raw data S3 bucket — CloudTrail data events + GuardDuty monitoring"
  default     = ""
}

variable "artifacts_bucket" {
  type        = string
  description = "Artifacts S3 bucket — CloudTrail data events"
  default     = ""
}

variable "sns_topic_arn_critical" {
  type        = string
  description = "SNS topic ARN for critical security alerts (from Phase 8 monitoring module)"
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
