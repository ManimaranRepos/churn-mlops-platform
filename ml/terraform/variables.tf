variable "project"     { type = string }
variable "environment" { type = string }
variable "aws_region"  { type = string }
variable "account_id"  { type = string }

variable "vpc_id"               { type = string }
variable "private_subnet_ids"   { type = list(string) }
variable "database_subnet_ids"  { type = list(string) }
variable "db_subnet_group_name" { type = string }
variable "node_security_group_id" { type = string }

variable "kms_rds_key_arn"      { type = string }
variable "kms_s3_key_arn"       { type = string }
variable "artifacts_bucket_name" { type = string }
variable "artifacts_bucket_arn"  { type = string }
variable "processed_bucket_name" { type = string }

variable "mlflow_db_secret_arn"  { type = string }

variable "aurora_instance_class" {
  type        = string
  description = "RDS instance class. Dev: db.t3.medium, Prod: db.r6g.large"
  default     = "db.t3.medium"
}

variable "aurora_min_capacity" {
  type    = number
  description = "Aurora Serverless v2 min ACUs (0.5 = ~1GB RAM)"
  default = 0.5
}

variable "aurora_max_capacity" {
  type    = number
  description = "Aurora Serverless v2 max ACUs"
  default = 4
}

variable "mlflow_image_tag" {
  type    = string
  default = "latest"
}
