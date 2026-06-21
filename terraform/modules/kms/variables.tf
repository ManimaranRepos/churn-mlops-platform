variable "project" {
  description = "Project name prefix"
  type        = string
}

variable "environment" {
  description = "Deployment environment (dev/staging/prod)"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "key_deletion_window_days" {
  description = "Days before a deleted KMS key is permanently removed (7-30)"
  type        = number
  default     = 30
}

variable "enable_key_rotation" {
  description = "Automatically rotate KMS keys annually"
  type        = bool
  default     = true
}
