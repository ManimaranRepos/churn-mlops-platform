variable "project"     { type = string }
variable "environment" { type = string }
variable "aws_region"  { type = string; default = "us-east-1" }
variable "account_id"  { type = string }

variable "vpc_id"              { type = string }
variable "vpc_cidr"            { type = string }
variable "private_subnet_ids"  { type = list(string) }

variable "sagemaker_role_arn"         { type = string }
variable "sagemaker_security_group_id" { type = string }
variable "sagemaker_endpoint_name"    { type = string; default = "" }

variable "artifacts_bucket" { type = string }
variable "raw_bucket"       { type = string }
variable "kms_key_arn"      { type = string }

variable "model_version" {
  type        = string
  description = "The model version string used to locate baseline artifacts in S3"
}

variable "model_threshold" {
  type        = number
  default     = 0.5
  description = "Churn probability threshold (from inference_metadata.json)"
}

variable "airflow_api_url" {
  type        = string
  description = "Airflow webserver internal URL for retraining trigger"
  default     = ""
}

variable "sns_topic_arn_warning" { type = string }
variable "sns_topic_arn_info"    { type = string; default = "" }

variable "tags" {
  type    = map(string)
  default = {}
}
