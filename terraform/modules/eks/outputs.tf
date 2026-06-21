output "cluster_name" {
  value       = aws_eks_cluster.main.name
  description = "EKS cluster name — used in kubectl commands and Helm deployments"
}

output "cluster_endpoint" {
  value       = aws_eks_cluster.main.endpoint
  description = "EKS API server endpoint"
}

output "cluster_ca_certificate" {
  value       = aws_eks_cluster.main.certificate_authority[0].data
  description = "Base64 encoded cluster CA certificate"
}

output "cluster_oidc_issuer_url" {
  value       = aws_eks_cluster.main.identity[0].oidc[0].issuer
  description = "OIDC issuer URL — used for IRSA role trust policies"
}

output "oidc_provider_arn" {
  value       = aws_iam_openid_connect_provider.eks.arn
  description = "OIDC provider ARN — used in IRSA trust policies for pod-level IAM"
}

output "node_security_group_id" {
  value       = aws_security_group.eks_nodes.id
  description = "Security group ID for EKS worker nodes"
}

output "cluster_security_group_id" {
  value       = aws_security_group.eks_cluster.id
  description = "Security group ID for EKS control plane"
}

output "alb_controller_role_arn" {
  value       = aws_iam_role.alb_controller.arn
  description = "IAM role ARN for AWS Load Balancer Controller — set in Helm values"
}

output "cluster_autoscaler_role_arn" {
  value       = aws_iam_role.cluster_autoscaler.arn
  description = "IAM role ARN for Cluster Autoscaler"
}

output "external_secrets_role_arn" {
  value       = aws_iam_role.external_secrets.arn
  description = "IAM role ARN for External Secrets Operator"
}

output "karpenter_controller_role_arn" {
  value       = aws_iam_role.karpenter_controller.arn
  description = "IAM role ARN for Karpenter controller"
}

output "karpenter_interruption_queue_url" {
  value       = aws_sqs_queue.karpenter_interruption.url
  description = "SQS queue URL for Karpenter spot interruption handling"
}

output "karpenter_interruption_queue_arn" {
  value       = aws_sqs_queue.karpenter_interruption.arn
}
