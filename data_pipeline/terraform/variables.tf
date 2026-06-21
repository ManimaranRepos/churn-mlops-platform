variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "account_id" { type = string }

variable "raw_bucket_name" {
  type        = string
  description = "S3 bucket for raw event data (from Phase 1 S3 module)"
}

variable "raw_bucket_arn" { type = string }

variable "processed_bucket_name" {
  type        = string
  description = "S3 bucket for curated Iceberg tables"
}

variable "processed_bucket_arn" { type = string }

variable "logs_bucket_name" { type = string }

variable "kms_s3_key_arn" {
  type        = string
  description = "KMS key for S3 and Kinesis encryption"
}

variable "lambda_role_arn" {
  type        = string
  description = "IAM role for Lambda functions (from Phase 1 IAM module)"
}

variable "glue_role_arn" {
  type        = string
  description = "IAM role for Glue jobs (from Phase 1 IAM module)"
}

variable "vpc_id" { type = string }

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets for Glue connections and Lambda VPC"
}

# ── Kinesis tuning ─────────────────────────────────────────────────────────
variable "kinesis_shard_count" {
  type        = number
  description = "Shards = throughput. Each shard = 1MB/s in, 2MB/s out. Dev: 1, Prod: 10+"
  default     = 1
}

variable "kinesis_retention_hours" {
  type        = number
  description = "How long to keep records in the stream (replay window). Max 8760 (1 year)."
  default     = 24
}

# ── Firehose tuning ────────────────────────────────────────────────────────
variable "firehose_buffer_size_mb" {
  type        = number
  description = "Accumulate this many MB before flushing to S3. Larger = fewer files = cheaper Athena queries."
  default     = 64
}

variable "firehose_buffer_interval_seconds" {
  type        = number
  description = "Flush to S3 after this many seconds even if buffer isn't full. Max 900 (15 min)."
  default     = 300
}

# ── Glue tuning ───────────────────────────────────────────────────────────
variable "glue_worker_type" {
  type        = string
  description = "G.1X = 4 vCPU/16GB, G.2X = 8 vCPU/32GB. Dev: G.1X, Prod: G.2X"
  default     = "G.1X"
}

variable "glue_num_workers" {
  type        = number
  description = "Number of Glue DPUs. More workers = faster ETL = higher cost."
  default     = 2
}
