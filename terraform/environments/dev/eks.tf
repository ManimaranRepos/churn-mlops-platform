# =============================================================================
# EKS — Dev Environment
# =============================================================================
# This file extends environments/dev/main.tf with the EKS cluster.
# Split into a separate file to keep main.tf readable as modules grow.
# =============================================================================

# Kubernetes and Helm providers — configure after EKS cluster exists
# They authenticate using the cluster endpoint + CA + token from AWS
provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_ca_certificate)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}

# =============================================================================
# EKS MODULE CALL
# Passes all required inputs from Phase 1 module outputs
# =============================================================================
module "eks" {
  source = "../../modules/eks"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region
  account_id  = data.aws_caller_identity.current.account_id

  # From Phase 1 VPC module
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  public_subnet_ids  = module.vpc.public_subnet_ids

  # From Phase 1 KMS module
  eks_kms_key_arn = module.kms.eks_key_arn

  # From Phase 1 IAM module
  node_role_arn = module.iam.eks_node_role_arn

  cluster_version = "1.29"

  # Dev: smaller instances to save cost
  general_instance_types   = ["m5.large", "m5a.large"]
  general_min_size         = 2
  general_max_size         = 6
  general_desired_size     = 2

  # Dev: GPU nodes only when actively training
  gpu_instance_types  = ["g4dn.xlarge"]
  gpu_min_size        = 0
  gpu_max_size        = 2
  gpu_desired_size    = 0

  # Dev: minimal inference capacity
  inference_instance_types = ["c5.large"]
  inference_min_size       = 1
  inference_max_size       = 4
  inference_desired_size   = 1

  # Dev: public endpoint OK (engineers access from laptops)
  # Prod: set to false and use VPN/bastion
  enable_cluster_endpoint_public_access = true
  cluster_endpoint_public_access_cidrs  = ["0.0.0.0/0"] # Lock to team IP range in real usage
}

# =============================================================================
# KUBERNETES NAMESPACES + POD SECURITY STANDARDS
# Created via the Kubernetes provider (not kubectl) so they're in Terraform state
# =============================================================================

locals {
  # Each namespace has a Pod Security Standard label.
  # baseline: blocks privileged pods, host namespaces (reasonable default)
  # restricted: additionally blocks root containers, privilege escalation
  namespaces = {
    mlops = {
      pss         = "baseline"
      description = "MLflow, model training orchestration"
    }
    "data-engineering" = {
      pss         = "baseline"
      description = "Glue triggers, Kinesis consumers, ETL coordination"
    }
    monitoring = {
      pss         = "baseline"
      description = "Prometheus, Grafana, alerting stack"
    }
    inference = {
      pss         = "restricted"
      description = "FastAPI model servers — highest security, no root containers"
    }
    airflow = {
      pss         = "baseline"
      description = "Apache Airflow scheduler, workers, webserver"
    }
    "external-secrets" = {
      pss         = "restricted"
      description = "External Secrets Operator — handles credentials"
    }
    karpenter = {
      pss         = "restricted"
      description = "Karpenter node provisioner"
    }
    argocd = {
      pss         = "baseline"
      description = "ArgoCD GitOps controller"
    }
  }
}

resource "kubernetes_namespace" "platform" {
  for_each = local.namespaces

  metadata {
    name = each.key

    labels = {
      name        = each.key
      environment = var.environment
      # Pod Security Standards enforcement
      # warn: shows warnings but allows, enforce: blocks non-compliant pods
      "pod-security.kubernetes.io/enforce"         = each.value.pss
      "pod-security.kubernetes.io/enforce-version" = "latest"
      "pod-security.kubernetes.io/warn"            = each.value.pss
      "pod-security.kubernetes.io/warn-version"    = "latest"
      "pod-security.kubernetes.io/audit"           = each.value.pss
      "pod-security.kubernetes.io/audit-version"   = "latest"
    }

    annotations = {
      "description" = each.value.description
    }
  }

  depends_on = [module.eks]
}

# =============================================================================
# HELM RELEASES — Core Platform Tools
# Deployed in dependency order: cert-manager first (others need TLS certs)
# =============================================================================

# ── cert-manager ──────────────────────────────────────────────────────────────
# Why: Issues TLS certificates for internal services automatically.
# Without it: manual cert rotation, services use self-signed certs.
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = "v1.14.4"
  namespace        = "cert-manager"
  create_namespace = true

  values = [file("${path.root}/../../../helm/base-platform/cert-manager/values.yaml")]

  set {
    name  = "installCRDs"
    value = "true" # Install Custom Resource Definitions with the chart
  }

  depends_on = [module.eks, kubernetes_namespace.platform]
}

# ── AWS Load Balancer Controller ──────────────────────────────────────────────
# Why: Translates Kubernetes Ingress/Service objects into AWS ALBs/NLBs.
# Without it: no way to expose services to the internet or internally.
resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "1.7.2"
  namespace  = "kube-system"

  values = [
    templatefile("${path.root}/../../../helm/base-platform/aws-load-balancer-controller/values.yaml", {
      cluster_name = module.eks.cluster_name
      role_arn     = module.eks.alb_controller_role_arn
      vpc_id       = module.vpc.vpc_id
      aws_region   = var.aws_region
    })
  ]

  depends_on = [helm_release.cert_manager]
}

# ── External Secrets Operator ─────────────────────────────────────────────────
# Why: Syncs AWS Secrets Manager → Kubernetes Secrets.
# Pods mount Kubernetes secrets; they never need AWS SDK credentials.
resource "helm_release" "external_secrets" {
  name       = "external-secrets"
  repository = "https://charts.external-secrets.io"
  chart      = "external-secrets"
  version    = "0.9.13"
  namespace  = "external-secrets"

  values = [
    templatefile("${path.root}/../../../helm/base-platform/external-secrets/values.yaml", {
      role_arn = module.eks.external_secrets_role_arn
    })
  ]

  depends_on = [kubernetes_namespace.platform]
}

# ── Metrics Server ────────────────────────────────────────────────────────────
# Why: Powers kubectl top and Horizontal Pod Autoscaler (HPA).
# Without it: HPA cannot read CPU/memory metrics → no autoscaling.
resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  version    = "3.12.0"
  namespace  = "kube-system"

  values = [file("${path.root}/../../../helm/base-platform/metrics-server/values.yaml")]

  depends_on = [module.eks]
}

# ── Cluster Autoscaler ────────────────────────────────────────────────────────
# Why: Scales node groups up when pods are Pending, down when nodes are idle.
# Complements Karpenter: CA handles managed node groups, Karpenter handles
# bespoke workloads that need specific instance types.
resource "helm_release" "cluster_autoscaler" {
  name       = "cluster-autoscaler"
  repository = "https://kubernetes.github.io/autoscaler"
  chart      = "cluster-autoscaler"
  version    = "9.36.0"
  namespace  = "kube-system"

  values = [
    templatefile("${path.root}/../../../helm/base-platform/cluster-autoscaler/values.yaml", {
      cluster_name = module.eks.cluster_name
      aws_region   = var.aws_region
      role_arn     = module.eks.cluster_autoscaler_role_arn
    })
  ]

  depends_on = [module.eks]
}

# ── Karpenter ─────────────────────────────────────────────────────────────────
# Why: More intelligent than Cluster Autoscaler for ML workloads.
# Karpenter can provision a specific GPU instance type for a training job
# in <60 seconds, and consolidate/terminate nodes when jobs finish.
resource "helm_release" "karpenter" {
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = "0.35.1"
  namespace  = "karpenter"

  values = [
    templatefile("${path.root}/../../../helm/base-platform/karpenter/values.yaml", {
      cluster_name      = module.eks.cluster_name
      cluster_endpoint  = module.eks.cluster_endpoint
      role_arn          = module.eks.karpenter_controller_role_arn
      interruption_queue = module.eks.karpenter_interruption_queue_url
    })
  ]

  depends_on = [kubernetes_namespace.platform, module.eks]
}

# =============================================================================
# OUTPUTS from EKS phase — needed by Phase 3+ (CI/CD, ArgoCD, monitoring)
# =============================================================================
output "eks_cluster_name" {
  value = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "eks_oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "alb_controller_role_arn" {
  value = module.eks.alb_controller_role_arn
}

output "karpenter_controller_role_arn" {
  value = module.eks.karpenter_controller_role_arn
}
