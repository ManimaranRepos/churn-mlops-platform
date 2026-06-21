# =============================================================================
# EKS MODULE — Cluster, Node Groups, Managed Add-ons, IRSA Roles
# =============================================================================
# Architecture decisions:
#   - Managed node groups (not self-managed): AWS handles node OS patching
#   - Three node groups: general, gpu-spot, inference — different workload profiles
#   - Private endpoint + public endpoint (dev only) — prod should be private-only
#   - KMS encryption of etcd secrets — Kubernetes secrets are sensitive
#   - IRSA for every AWS-calling component — no shared node-level permissions
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

locals {
  cluster_name = "${var.project}-${var.environment}-eks"
  name_prefix  = "${var.project}-${var.environment}"

  # Common tags applied to all node group resources
  # Cluster Autoscaler discovers node groups via these tags
  common_node_tags = {
    "k8s.io/cluster-autoscaler/enabled"         = "true"
    "k8s.io/cluster-autoscaler/${local.cluster_name}" = "owned"
  }
}

# =============================================================================
# EKS CONTROL PLANE
# =============================================================================
resource "aws_eks_cluster" "main" {
  name    = local.cluster_name
  version = var.cluster_version

  # The cluster role allows EKS to make AWS API calls on your behalf
  # (create ENIs, describe subnets, manage load balancers, etc.)
  role_arn = aws_iam_role.eks_cluster.arn

  vpc_config {
    subnet_ids = concat(var.private_subnet_ids, var.public_subnet_ids)

    # Security group for the control plane ENIs placed in your VPC
    security_group_ids = [aws_security_group.eks_cluster.id]

    # Private: nodes talk to control plane over private network
    endpoint_private_access = true
    # Public: kubectl from developer laptops (disable in prod)
    endpoint_public_access  = var.enable_cluster_endpoint_public_access
    public_access_cidrs     = var.cluster_endpoint_public_access_cidrs
  }

  # Encrypt Kubernetes secrets at rest using our CMK
  # Without this, secrets are stored in etcd in base64 (not encrypted)
  encryption_config {
    provider {
      key_arn = var.eks_kms_key_arn
    }
    resources = ["secrets"]
  }

  # Send control plane logs to CloudWatch
  # audit: who did what via kubectl (security)
  # api: API server errors
  # authenticator: IAM authentication events
  enabled_cluster_log_types = ["audit", "api", "authenticator", "controllerManager", "scheduler"]

  tags = {
    Name = local.cluster_name
  }

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
    aws_iam_role_policy_attachment.eks_vpc_resource_controller,
  ]
}

# =============================================================================
# CLUSTER IAM ROLE
# Assumed by: the EKS control plane itself (not nodes — nodes use node_role)
# =============================================================================
resource "aws_iam_role" "eks_cluster" {
  name = "${local.name_prefix}-eks-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  role       = aws_iam_role.eks_cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "eks_vpc_resource_controller" {
  role       = aws_iam_role.eks_cluster.name
  # Required for EKS to manage ENIs for pod networking
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
}

# =============================================================================
# CLUSTER SECURITY GROUP
# Controls what can talk to the EKS API server (control plane)
# =============================================================================
resource "aws_security_group" "eks_cluster" {
  name        = "${local.name_prefix}-eks-cluster-sg"
  description = "EKS cluster control plane security group"
  vpc_id      = var.vpc_id

  ingress {
    description = "Nodes to control plane (API server)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    # Nodes send API calls (kubelet, kube-proxy) to port 443
    security_groups = [aws_security_group.eks_nodes.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-eks-cluster-sg"
  }
}

# Node security group — controls node-to-node and node-to-control-plane traffic
resource "aws_security_group" "eks_nodes" {
  name        = "${local.name_prefix}-eks-nodes-sg"
  description = "EKS worker nodes security group"
  vpc_id      = var.vpc_id

  ingress {
    description = "Node to node — pods talking to pods on other nodes"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  ingress {
    description = "Control plane to nodes (kubelet, metrics)"
    from_port   = 1025
    to_port     = 65535
    protocol    = "tcp"
    security_groups = [aws_security_group.eks_cluster.id]
  }

  ingress {
    description = "Webhook ports — cert-manager, admission controllers"
    from_port   = 8443
    to_port     = 8443
    protocol    = "tcp"
    security_groups = [aws_security_group.eks_cluster.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_node_tags, {
    Name = "${local.name_prefix}-eks-nodes-sg"
    # Karpenter uses this to find the right SG for new nodes it launches
    "karpenter.sh/discovery" = local.cluster_name
  })
}

# =============================================================================
# MANAGED NODE GROUPS
# "Managed" means AWS handles: node provisioning, AMI updates, draining before
# termination. We just define the desired configuration.
# =============================================================================

# ── General-Purpose Node Group ────────────────────────────────────────────────
# Runs: Airflow, MLflow, monitoring, ArgoCD, platform services
resource "aws_eks_node_group" "general" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${local.name_prefix}-general"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.private_subnet_ids

  instance_types = var.general_instance_types

  scaling_config {
    min_size     = var.general_min_size
    max_size     = var.general_max_size
    desired_size = var.general_desired_size
  }

  # SPOT saves ~60% but nodes can be reclaimed with 2 min notice.
  # General services should handle this via pod disruption budgets.
  # Use ON_DEMAND for prod critical services.
  capacity_type = "SPOT"

  update_config {
    # During node upgrades, replace up to 33% of nodes simultaneously
    # Balances upgrade speed vs availability risk
    max_unavailable_percentage = 33
  }

  # Labels are used by pod nodeSelector/affinity rules
  labels = {
    role        = "general"
    environment = var.environment
  }

  taint {
    # No taint on general nodes — all pods can schedule here unless they
    # specifically request GPU or inference nodes
    key    = "dedicated"
    value  = "general"
    effect = "PREFER_NO_SCHEDULE"
  }

  tags = merge(local.common_node_tags, {
    Name = "${local.name_prefix}-general-node-group"
  })

  lifecycle {
    # Prevents Terraform from resetting desired_size after Cluster Autoscaler
    # changes it. Without this, every `terraform apply` would fight CA.
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# ── GPU Spot Node Group ───────────────────────────────────────────────────────
# Runs: ML training jobs (XGBoost, PyTorch), GPU preprocessing
# Starts at 0 — scales up only when training jobs are submitted
resource "aws_eks_node_group" "gpu" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${local.name_prefix}-gpu-spot"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.private_subnet_ids

  instance_types = var.gpu_instance_types
  capacity_type  = "SPOT" # GPU Spot = 60-80% savings. Training jobs checkpoint so loss is minimal.

  # AWS GPU-optimized AMI — includes NVIDIA drivers pre-installed
  ami_type = "AL2_x86_64_GPU"

  scaling_config {
    min_size     = var.gpu_min_size
    max_size     = var.gpu_max_size
    desired_size = var.gpu_desired_size
  }

  update_config {
    max_unavailable_percentage = 50
  }

  labels = {
    role             = "gpu-training"
    "nvidia.com/gpu" = "true"
    environment      = var.environment
  }

  # Taint ensures ONLY pods that explicitly tolerate this taint land here.
  # Prevents non-GPU workloads from accidentally consuming expensive GPU nodes.
  taint {
    key    = "nvidia.com/gpu"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  tags = merge(local.common_node_tags, {
    Name = "${local.name_prefix}-gpu-spot-node-group"
  })

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# ── Inference-Optimized Node Group ───────────────────────────────────────────
# Runs: FastAPI model servers — needs consistent latency, NOT spot instances
# Why ON_DEMAND here? A spot interruption on an inference node drops live
# user requests. On-Demand guarantees the node stays up.
resource "aws_eks_node_group" "inference" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${local.name_prefix}-inference"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.private_subnet_ids

  instance_types = var.inference_instance_types
  capacity_type  = "ON_DEMAND" # Predictable latency — no interruptions

  scaling_config {
    min_size     = var.inference_min_size
    max_size     = var.inference_max_size
    desired_size = var.inference_desired_size
  }

  update_config {
    max_unavailable_percentage = 25 # Conservative — minimize inference downtime
  }

  labels = {
    role        = "inference"
    environment = var.environment
  }

  taint {
    key    = "dedicated"
    value  = "inference"
    effect = "NO_SCHEDULE"
  }

  tags = merge(local.common_node_tags, {
    Name = "${local.name_prefix}-inference-node-group"
  })

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# =============================================================================
# EKS MANAGED ADD-ONS
# These are AWS-managed versions of core Kubernetes components.
# AWS keeps them patched and compatible with the cluster version.
# =============================================================================

# VPC CNI — manages pod networking (assigns VPC IPs to pods)
resource "aws_eks_addon" "vpc_cni" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "vpc-cni"
  addon_version            = "v1.16.0-eksbuild.1"
  resolve_conflicts_on_update = "OVERWRITE"

  # VPC CNI needs its own IAM role (IRSA) to manage ENIs
  service_account_role_arn = aws_iam_role.vpc_cni.arn

  configuration_values = jsonencode({
    env = {
      # Enable prefix delegation: each node can support 110 pods instead of ~30
      # by assigning /28 CIDR prefixes to ENIs instead of individual IPs
      ENABLE_PREFIX_DELEGATION = "true"
      WARM_PREFIX_TARGET       = "1"
    }
  })
}

# CoreDNS — internal DNS resolution (service.namespace.svc.cluster.local)
resource "aws_eks_addon" "coredns" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "coredns"
  addon_version            = "v1.11.1-eksbuild.4"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.general] # Needs nodes to schedule on
}

# kube-proxy — handles Kubernetes service networking (iptables/ipvs rules)
resource "aws_eks_addon" "kube_proxy" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "kube-proxy"
  addon_version            = "v1.29.0-eksbuild.1"
  resolve_conflicts_on_update = "OVERWRITE"
}

# EBS CSI Driver — allows pods to use EBS volumes as PersistentVolumes
# Required for: MLflow database storage, Prometheus data retention
resource "aws_eks_addon" "ebs_csi" {
  cluster_name             = aws_eks_cluster.main.name
  addon_name               = "aws-ebs-csi-driver"
  addon_version            = "v1.28.0-eksbuild.1"
  resolve_conflicts_on_update = "OVERWRITE"

  service_account_role_arn = aws_iam_role.ebs_csi.arn
}

# =============================================================================
# OIDC PROVIDER — Required for IRSA (IAM Roles for Service Accounts)
# EKS control plane has a built-in OIDC issuer. We register it with AWS IAM
# so that Kubernetes service accounts can assume IAM roles.
# =============================================================================
data "tls_certificate" "eks" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

# =============================================================================
# IRSA ROLES — One per AWS-calling component
# Pattern: IAM role with trust policy that says
# "only the Kubernetes service account X in namespace Y can assume me"
# =============================================================================

locals {
  oidc_provider_arn = aws_iam_openid_connect_provider.eks.arn
  # Extract just the hostname from the OIDC URL for condition matching
  oidc_provider     = replace(aws_iam_openid_connect_provider.eks.url, "https://", "")
}

# Helper for building IRSA trust policies
locals {
  irsa_trust_policy = { for sa in [
    { ns = "kube-system",        name = "aws-node" },           # VPC CNI
    { ns = "kube-system",        name = "ebs-csi-controller-sa" }, # EBS CSI
    { ns = "kube-system",        name = "cluster-autoscaler" },  # Cluster Autoscaler
    { ns = "kube-system",        name = "aws-load-balancer-controller" }, # ALB Controller
    { ns = "external-secrets",   name = "external-secrets-sa" }, # External Secrets
    { ns = "karpenter",          name = "karpenter" },           # Karpenter
  ] : "${sa.ns}/${sa.name}" => sa }
}

# ── VPC CNI IRSA ──────────────────────────────────────────────────────────────
resource "aws_iam_role" "vpc_cni" {
  name = "${local.name_prefix}-vpc-cni-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:kube-system:aws-node"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "vpc_cni" {
  role       = aws_iam_role.vpc_cni.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

# ── EBS CSI IRSA ──────────────────────────────────────────────────────────────
resource "aws_iam_role" "ebs_csi" {
  name = "${local.name_prefix}-ebs-csi-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# ── AWS Load Balancer Controller IRSA ────────────────────────────────────────
# Creates Application Load Balancers from Kubernetes Ingress resources
resource "aws_iam_role" "alb_controller" {
  name = "${local.name_prefix}-alb-controller-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

# ALB Controller needs extensive EC2/ELB permissions — use the AWS-provided policy
resource "aws_iam_role_policy" "alb_controller" {
  name = "${local.name_prefix}-alb-controller-policy"
  role = aws_iam_role.alb_controller.id

  policy = file("${path.module}/policies/alb-controller-policy.json")
}

# ── Cluster Autoscaler IRSA ───────────────────────────────────────────────────
# Scales node groups up/down based on pending pods / underutilized nodes
resource "aws_iam_role" "cluster_autoscaler" {
  name = "${local.name_prefix}-cluster-autoscaler-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:kube-system:cluster-autoscaler"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "cluster_autoscaler" {
  name = "${local.name_prefix}-cluster-autoscaler-policy"
  role = aws_iam_role.cluster_autoscaler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations",
          "autoscaling:DescribeScalingActivities",
          "autoscaling:DescribeTags",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplateVersions"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "autoscaling:SetDesiredCapacity",
          "autoscaling:TerminateInstanceInAutoScalingGroup",
          "ec2:DescribeImages",
          "ec2:GetInstanceTypesFromInstanceRequirements",
          "eks:DescribeNodegroup"
        ]
        # Scope to only this cluster's node groups via tag condition
        Resource = "*"
        Condition = {
          StringEquals = {
            "autoscaling:ResourceTag/k8s.io/cluster-autoscaler/enabled" = "true"
            "autoscaling:ResourceTag/k8s.io/cluster-autoscaler/${local.cluster_name}" = "owned"
          }
        }
      }
    ]
  })
}

# ── External Secrets IRSA ─────────────────────────────────────────────────────
# Syncs AWS Secrets Manager secrets into Kubernetes Secret objects
# Pods reference Kubernetes secrets — no AWS SDK needed in application code
resource "aws_iam_role" "external_secrets" {
  name = "${local.name_prefix}-external-secrets-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:external-secrets:external-secrets-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "external_secrets" {
  name = "${local.name_prefix}-external-secrets-policy"
  role = aws_iam_role.external_secrets.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:ListSecretVersionIds"
      ]
      # Only allow access to this project's secrets, not everything in the account
      Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${var.project}-${var.environment}/*"
    }]
  })
}

# ── Karpenter IRSA ────────────────────────────────────────────────────────────
# Karpenter provisions individual EC2 instances (not ASGs) — needs broad EC2 permissions
resource "aws_iam_role" "karpenter_controller" {
  name = "${local.name_prefix}-karpenter-controller-irsa"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:sub" = "system:serviceaccount:karpenter:karpenter"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "karpenter_controller" {
  name = "${local.name_prefix}-karpenter-controller-policy"
  role = aws_iam_role.karpenter_controller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "KarpenterEC2"
        Effect = "Allow"
        Action = [
          "ec2:CreateLaunchTemplate",
          "ec2:CreateFleet",
          "ec2:RunInstances",
          "ec2:CreateTags",
          "ec2:TerminateInstances",
          "ec2:DeleteLaunchTemplate",
          "ec2:DescribeLaunchTemplates",
          "ec2:DescribeInstances",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeSpotPriceHistory"
        ]
        Resource = "*"
      },
      {
        Sid    = "KarpenterSSM"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:*:*:parameter/aws/service/*"
      },
      {
        Sid    = "KarpenterIAMPassRole"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = "arn:aws:iam::${var.account_id}:role/${local.name_prefix}-eks-node-role"
      },
      {
        Sid    = "KarpenterSQS"
        Effect = "Allow"
        Action = [
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ReceiveMessage"
        ]
        # Karpenter listens to this SQS queue for Spot interruption notices
        Resource = aws_sqs_queue.karpenter_interruption.arn
      }
    ]
  })
}

# =============================================================================
# KARPENTER INTERRUPTION HANDLING
# When AWS reclaims a Spot instance, it sends a 2-minute warning.
# Karpenter listens via SQS and proactively drains the node before termination.
# Without this, pods are abruptly killed. With this, they gracefully migrate.
# =============================================================================
resource "aws_sqs_queue" "karpenter_interruption" {
  name                      = "${local.name_prefix}-karpenter-interruption"
  message_retention_seconds = 300 # 5 minutes — interruption events are time-sensitive

  tags = {
    Name = "${local.name_prefix}-karpenter-interruption"
  }
}

# EventBridge sends Spot interruption events to the SQS queue
resource "aws_cloudwatch_event_rule" "spot_interruption" {
  name        = "${local.name_prefix}-spot-interruption"
  description = "Capture Spot instance interruption warnings for Karpenter"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Spot Instance Interruption Warning"]
  })
}

resource "aws_cloudwatch_event_target" "spot_interruption" {
  rule      = aws_cloudwatch_event_rule.spot_interruption.name
  target_id = "KarpenterInterruptionQueue"
  arn       = aws_sqs_queue.karpenter_interruption.arn
}

# Also capture instance state changes (for faster node lifecycle management)
resource "aws_cloudwatch_event_rule" "instance_state_change" {
  name        = "${local.name_prefix}-instance-state-change"
  description = "Capture EC2 instance state changes for Karpenter"

  event_pattern = jsonencode({
    source      = ["aws.ec2"]
    detail-type = ["EC2 Instance State-change Notification"]
  })
}

resource "aws_cloudwatch_event_target" "instance_state_change" {
  rule      = aws_cloudwatch_event_rule.instance_state_change.name
  target_id = "KarpenterInterruptionQueue"
  arn       = aws_sqs_queue.karpenter_interruption.arn
}

resource "aws_sqs_queue_policy" "karpenter_interruption" {
  queue_url = aws_sqs_queue.karpenter_interruption.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.karpenter_interruption.arn
    }]
  })
}

# =============================================================================
# STORAGE CLASSES
# We define custom StorageClasses beyond the EKS default.
# =============================================================================

# We'll apply these via kubectl after cluster creation (see deploy commands)
# Stored here as a local_file for reference
resource "local_file" "storage_classes" {
  filename = "${path.module}/manifests/storage-classes.yaml"
  content  = <<-YAML
    # gp3 is 20% cheaper and faster than gp2 — set as default
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: gp3
      annotations:
        storageclass.kubernetes.io/is-default-class: "true"
    provisioner: ebs.csi.aws.com
    parameters:
      type: gp3
      iops: "3000"
      throughput: "125"
      encrypted: "true"
      kmsKeyId: "${var.eks_kms_key_arn}"
    reclaimPolicy: Delete
    volumeBindingMode: WaitForFirstConsumer  # Don't provision until pod is scheduled
    allowVolumeExpansion: true
    ---
    # io1 for MLflow/Airflow databases that need consistent IOPS
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: io1-high-iops
    provisioner: ebs.csi.aws.com
    parameters:
      type: io1
      iopsPerGB: "50"
      encrypted: "true"
      kmsKeyId: "${var.eks_kms_key_arn}"
    reclaimPolicy: Retain  # Don't delete data on PVC deletion — safer for databases
    volumeBindingMode: WaitForFirstConsumer
    allowVolumeExpansion: true
  YAML
}
