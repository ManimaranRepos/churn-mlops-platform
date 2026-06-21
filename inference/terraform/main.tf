# =============================================================================
# Inference pod IAM (IRSA) — grants the FastAPI pods AWS permissions
#
# The inference pods need:
#   - Secrets Manager: read MLflow tracking URI and Redis URL
#   - S3: download model artifacts from MLflow artifact store
#   - CloudWatch: emit custom metrics (prediction latency, cache hit rate)
#   - ECR: pull their own image (handled by the node role, not pod role —
#          listed here for documentation completeness only)
#
# WHY IRSA (not environment variables with hardcoded credentials)?
#   IRSA issues short-lived STS tokens (15 min TTL) to each pod. If a pod is
#   compromised, the blast radius is: (a) limited to those permissions only,
#   and (b) the token expires quickly. Hardcoded credentials in env vars would
#   be persistent and would rotate manually.
# =============================================================================

resource "aws_iam_role" "inference" {
  name = "${var.project}-${var.environment}-inference-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.eks_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.eks_oidc_provider_url}:sub" = "system:serviceaccount:inference:churn-inference"
          "${var.eks_oidc_provider_url}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "inference" {
  name = "${var.project}-${var.environment}-inference-policy"
  role = aws_iam_role.inference.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:churn-platform/mlflow/*",
          "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:churn-platform/elasticache/*",
        ]
      },
      {
        Sid    = "S3ModelArtifacts"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.artifacts_bucket}",
          "arn:aws:s3:::${var.artifacts_bucket}/mlflow/*",
        ]
      },
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "ChurnPlatform/Inference"
          }
        }
      },
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.kms_key_arn]
      }
    ]
  })
}

# CloudWatch log group for inference pod logs (structured JSON logs from uvicorn)
resource "aws_cloudwatch_log_group" "inference" {
  name              = "/aws/eks/${var.project}-${var.environment}/inference"
  retention_in_days = 14
  kms_key_id        = var.kms_key_arn

  tags = var.tags
}
