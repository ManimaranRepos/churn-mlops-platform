variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "vpc_id" {
  type        = string
  description = "VPC ID from the vpc module output"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for EKS nodes — nodes must be private"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Public subnet IDs — used only for public-facing load balancers"
}

variable "cluster_version" {
  type        = string
  description = "EKS Kubernetes version. Pin this — upgrades require testing."
  default     = "1.29"
}

variable "eks_kms_key_arn" {
  type        = string
  description = "KMS key ARN for encrypting Kubernetes secrets at rest"
}

variable "node_role_arn" {
  type        = string
  description = "IAM role ARN for EKS worker nodes (from iam module)"
}

# ── General-purpose node group ────────────────────────────────────────────────
# Runs: Airflow workers, MLflow server, monitoring stack, misc services
variable "general_instance_types" {
  type    = list(string)
  default = ["m5.xlarge", "m5a.xlarge"] # Two types = better Spot availability
}

variable "general_min_size" {
  type    = number
  default = 2 # Always keep 2 up for HA
}

variable "general_max_size" {
  type    = number
  default = 10
}

variable "general_desired_size" {
  type    = number
  default = 3
}

# ── GPU Spot node group ───────────────────────────────────────────────────────
# Runs: SageMaker-attached training pods, GPU-accelerated preprocessing
# Why Spot? Training jobs are fault-tolerant (SageMaker checkpoints).
# Spot saves 60-90% vs On-Demand for GPU instances.
variable "gpu_instance_types" {
  type    = list(string)
  default = ["g4dn.xlarge", "g4dn.2xlarge"] # NVIDIA T4 GPUs
}

variable "gpu_min_size" {
  type    = number
  default = 0 # Scale to zero when no training jobs running
}

variable "gpu_max_size" {
  type    = number
  default = 5
}

variable "gpu_desired_size" {
  type    = number
  default = 0
}

# ── Inference node group ──────────────────────────────────────────────────────
# Runs: FastAPI model servers, real-time inference pods
# Why separate? Inference needs predictable latency — we don't want a
# noisy-neighbour training job competing for CPU on the same node.
variable "inference_instance_types" {
  type    = list(string)
  default = ["c5.2xlarge", "c5.xlarge"] # Compute-optimized for inference math
}

variable "inference_min_size" {
  type    = number
  default = 1
}

variable "inference_max_size" {
  type    = number
  default = 20
}

variable "inference_desired_size" {
  type    = number
  default = 2
}

variable "enable_cluster_endpoint_public_access" {
  type        = bool
  description = "Allow kubectl from internet. Set false in prod — use VPN/bastion instead."
  default     = true # True in dev for convenience; must be false in prod
}

variable "cluster_endpoint_public_access_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the public endpoint. Lock down to your IP in dev."
  default     = ["0.0.0.0/0"] # Restrict in prod!
}
