variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "account_id" {
  type        = string
  description = "AWS account ID for IAM ARN construction"
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Subnets for VPC Link ENIs — must be in same VPC as ALB"
}

variable "alb_listener_arn" {
  type        = string
  description = "ARN of the internal ALB listener (port 80) that routes to inference pods"
}

variable "allowed_origins" {
  type        = list(string)
  default     = ["*"]
  description = "CORS allowed origins — restrict in prod to your CRM domain"
}

variable "sns_topic_arn" {
  type        = string
  description = "SNS topic for CloudWatch alarm notifications"
}

variable "tags" {
  type    = map(string)
  default = {}
}
