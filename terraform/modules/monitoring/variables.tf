variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string; default = "us-east-1" }
variable "account_id" { type = string }

variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "kms_key_arn" { type = string }

variable "eks_oidc_provider_arn" { type = string }
variable "eks_oidc_provider_url" { type = string }

variable "alertmanager_irsa_role_arn" {
  type        = string
  description = "IRSA role used by AlertManager pod — allowed to publish to SNS topics"
}

variable "api_gateway_id" {
  type        = string
  description = "HTTP API v2 ID for CloudWatch dashboard"
}

variable "elasticache_cluster_id" {
  type        = string
  description = "ElastiCache replication group ID for CloudWatch dashboard"
}

variable "kinesis_stream_name" {
  type        = string
  description = "Kinesis stream name for CloudWatch dashboard"
}

variable "grafana_url" {
  type        = string
  default     = "https://grafana.internal.churn-platform.example.com"
  description = "Grafana URL for dashboard link in CloudWatch dashboard header"
}

variable "slack_channel_critical" {
  type    = string
  default = "#alerts-critical"
}

variable "slack_channel_warning" {
  type    = string
  default = "#alerts-warning"
}

variable "slack_channel_info" {
  type    = string
  default = "#ml-platform"
}

variable "tags" {
  type    = map(string)
  default = {}
}
