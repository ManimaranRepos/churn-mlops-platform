variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "account_id" {
  type        = string
  description = "AWS account ID — used for globally unique bucket naming"
}

variable "kms_key_arn" {
  type        = string
  description = "KMS key ARN for S3 server-side encryption"
}

variable "raw_data_retention_days" {
  type        = number
  description = "Days to retain raw event data before transitioning to Glacier"
  default     = 365
}

variable "processed_data_retention_days" {
  type        = number
  description = "Days to retain processed/curated data"
  default     = 730
}

variable "model_artifacts_retention_days" {
  type        = number
  description = "Days to retain model artifacts before archiving"
  default     = 365
}

variable "log_retention_days" {
  type        = number
  description = "Days to retain access logs"
  default     = 90
}
