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

variable "vpc_cidr" {
  description = "CIDR block for the VPC. Use /16 to give plenty of room for subnets."
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  description = "List of AZs to deploy into. Always use 3 for production resilience."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

# /19 gives 8190 IPs per subnet — plenty for EKS pods which consume IPs fast
# (each pod gets its own IP in AWS VPC CNI mode)
variable "public_subnet_cidrs" {
  description = "CIDRs for public subnets (one per AZ)"
  type        = list(string)
  default     = ["10.0.0.0/19", "10.0.32.0/19", "10.0.64.0/19"]
}

variable "private_subnet_cidrs" {
  description = "CIDRs for private subnets (one per AZ) — EKS nodes, RDS, etc."
  type        = list(string)
  default     = ["10.0.96.0/19", "10.0.128.0/19", "10.0.160.0/19"]
}

variable "database_subnet_cidrs" {
  description = "CIDRs for isolated database subnets — no route to internet even via NAT"
  type        = list(string)
  default     = ["10.0.192.0/21", "10.0.200.0/21", "10.0.208.0/21"]
}

variable "flow_logs_retention_days" {
  description = "How many days to retain VPC Flow Logs in CloudWatch"
  type        = number
  default     = 30
}

variable "cloudwatch_kms_key_arn" {
  description = "KMS key ARN for CloudWatch log group encryption"
  type        = string
}

variable "enable_nat_gateway" {
  description = "Whether to create NAT Gateways. Set false to save cost in pure-dev environments."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Use one NAT Gateway instead of one-per-AZ. Saves ~$100/month but loses HA. Dev only."
  type        = bool
  default     = false
}
