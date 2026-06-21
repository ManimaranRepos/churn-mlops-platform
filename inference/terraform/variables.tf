variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string; default = "us-east-1" }
variable "account_id" { type = string }
variable "eks_oidc_provider_arn" { type = string }
variable "eks_oidc_provider_url" { type = string }
variable "artifacts_bucket" { type = string }
variable "kms_key_arn" { type = string }
variable "tags" { type = map(string); default = {} }
