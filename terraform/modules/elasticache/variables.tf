variable "project" {
  type    = string
}

variable "environment" {
  type    = string
}

variable "vpc_id" {
  type    = string
}

variable "private_subnet_ids" {
  type    = list(string)
}

variable "eks_worker_security_group_id" {
  type        = string
  description = "Security group of EKS worker nodes — Redis ingress allowed from here"
}

variable "node_type" {
  type        = string
  default     = "cache.t4g.small"
  description = "ElastiCache node type. t4g.small (~1.4GB) is sufficient for prediction cache"
}

variable "sns_topic_arn" {
  type        = string
  description = "SNS topic for CloudWatch alarm notifications"
}

variable "tags" {
  type    = map(string)
  default = {}
}
