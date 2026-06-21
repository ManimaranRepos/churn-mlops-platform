# =============================================================================
# IAM MODULE — Least-Privilege Roles
# =============================================================================
# One role per workload type. Each role's trust policy defines WHO can assume
# it; each role's permission policy defines WHAT it can do.
#
# Roles created:
#   1. EKS Node Role     — EC2 instances in EKS node groups
#   2. EKS Pod Role      — base role for IRSA (per-pod IAM via service accounts)
#   3. SageMaker Role    — training jobs, endpoints, model monitor
#   4. Glue Role         — ETL jobs, crawlers
#   5. Lambda Role       — Kinesis consumers, Firehose transformer, drift detector
#   6. CI/CD Role        — GitHub Actions OIDC (no long-lived access keys!)
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# -----------------------------------------------------------------------------
# 1. EKS NODE ROLE
# Assumed by: EC2 instances (the EKS worker nodes themselves)
# What it needs: ability to join the cluster, pull images from ECR,
# send logs to CloudWatch, and manage its own networking (VPC CNI)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "eks_node" {
  name        = "${local.name_prefix}-eks-node-role"
  description = "Role assumed by EKS worker nodes (EC2 instances)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# AWS-managed policies that EKS nodes require — these are non-negotiable
resource "aws_iam_role_policy_attachment" "eks_node_policy" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "eks_cni_policy" {
  role       = aws_iam_role.eks_node.name
  # VPC CNI manages pod networking — assigns IPs from the VPC subnet to pods
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "eks_ecr_readonly" {
  role       = aws_iam_role.eks_node.name
  # Nodes pull Docker images from ECR — they need read access
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "eks_cloudwatch" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# SSM access — allows engineers to shell into nodes without SSH keys
# This is the "no SSH" security pattern: SSM Session Manager instead
resource "aws_iam_role_policy_attachment" "eks_ssm" {
  role       = aws_iam_role.eks_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Instance profile wraps the role so EC2 can assume it
resource "aws_iam_instance_profile" "eks_node" {
  name = "${local.name_prefix}-eks-node-instance-profile"
  role = aws_iam_role.eks_node.name
}

# -----------------------------------------------------------------------------
# 2. SAGEMAKER ROLE
# Assumed by: SageMaker training jobs, processing jobs, endpoints
# Needs: S3 read/write for data and artifacts, KMS decrypt, CloudWatch logs,
# ECR pull for custom training containers
# -----------------------------------------------------------------------------
resource "aws_iam_role" "sagemaker" {
  name        = "${local.name_prefix}-sagemaker-role"
  description = "Role for SageMaker training jobs and inference endpoints"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sagemaker.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sagemaker_s3" {
  name = "${local.name_prefix}-sagemaker-s3-policy"
  role = aws_iam_role.sagemaker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadRawData"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          var.raw_bucket_arn,
          "${var.raw_bucket_arn}/*",
          var.processed_bucket_arn,
          "${var.processed_bucket_arn}/*"
        ]
      },
      {
        Sid    = "WriteModelArtifacts"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [
          var.artifacts_bucket_arn,
          "${var.artifacts_bucket_arn}/*"
        ]
      },
      {
        Sid    = "KMSDecryptForS3"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [var.kms_s3_key_arn]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "sagemaker_full" {
  role       = aws_iam_role.sagemaker.name
  # SageMaker needs broad permissions to manage its own resources
  # We scope down data access via the custom policy above
  policy_arn = "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
}

# -----------------------------------------------------------------------------
# 3. GLUE ROLE
# Assumed by: Glue ETL jobs, Glue crawlers
# Needs: read raw S3, write processed S3, access Glue Data Catalog
# -----------------------------------------------------------------------------
resource "aws_iam_role" "glue" {
  name        = "${local.name_prefix}-glue-role"
  description = "Role for Glue ETL jobs and crawlers"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_s3" {
  name = "${local.name_prefix}-glue-s3-policy"
  role = aws_iam_role.glue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadRawData"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.raw_bucket_arn, "${var.raw_bucket_arn}/*"]
      },
      {
        Sid    = "WriteProcessedData"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [var.processed_bucket_arn, "${var.processed_bucket_arn}/*"]
      },
      {
        Sid      = "KMSAccess"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [var.kms_s3_key_arn]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# 4. LAMBDA ROLE
# Assumed by: event ingestion Lambda, Firehose transformer, drift detector
# Deliberately scoped — Lambda cannot write to model artifacts or RDS
# -----------------------------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name        = "${local.name_prefix}-lambda-role"
  description = "Base role for Lambda functions in the churn platform"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  # Gives: CloudWatch Logs write access + basic execution rights
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  # Required for Lambda functions running inside the VPC
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "lambda_platform" {
  name = "${local.name_prefix}-lambda-platform-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "KinesisAccess"
        Effect = "Allow"
        Action = [
          "kinesis:GetRecords",
          "kinesis:GetShardIterator",
          "kinesis:DescribeStream",
          "kinesis:ListStreams",
          "kinesis:PutRecord",
          "kinesis:PutRecords"
        ]
        Resource = "arn:aws:kinesis:${var.aws_region}:${var.account_id}:stream/${local.name_prefix}-*"
      },
      {
        Sid    = "S3RawWrite"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = ["${var.raw_bucket_arn}/*"]
      },
      {
        Sid    = "SecretsManagerRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        # Lambda can only read secrets prefixed with the project name
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:${local.name_prefix}/*"
      },
      {
        Sid      = "KMSDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.kms_s3_key_arn, var.kms_secrets_key_arn]
      },
      {
        Sid    = "SQSAccess"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = "arn:aws:sqs:${var.aws_region}:${var.account_id}:${local.name_prefix}-*"
      },
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "${local.name_prefix}"
          }
        }
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# 5. CI/CD ROLE — GitHub Actions OIDC
# Why OIDC instead of access keys?
# Long-lived access keys are a major security risk — they're often leaked
# in git commits or environment variables. OIDC gives GitHub Actions temporary
# credentials that expire after the job ends. No keys to rotate or leak.
# -----------------------------------------------------------------------------
data "aws_iam_openid_connect_provider" "github" {
  # This will fail if you haven't set up the GitHub OIDC provider yet.
  # Run the bootstrap_github_oidc script first (see docs/onboarding.md)
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "cicd" {
  name        = "${local.name_prefix}-cicd-role"
  description = "Role assumed by GitHub Actions via OIDC — no long-lived keys"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = data.aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Restrict to your specific GitHub org/repo — wildcards allowed
          # Change this to your actual GitHub org and repo name
          "token.actions.githubusercontent.com:sub" = "repo:your-org/${var.project}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "cicd" {
  name = "${local.name_prefix}-cicd-policy"
  role = aws_iam_role.cicd.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformStateAccess"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        # CI/CD only touches Terraform state — not the data lake
        Resource = [
          "arn:aws:s3:::${var.project}-terraform-state-${var.account_id}",
          "arn:aws:s3:::${var.project}-terraform-state-${var.account_id}/*"
        ]
      },
      {
        Sid      = "TerraformLockTable"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:${var.aws_region}:${var.account_id}:table/${var.project}-terraform-locks"
      },
      {
        Sid    = "ECRPushPull"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload"
        ]
        Resource = "*"
      },
      {
        Sid    = "EKSDescribe"
        Effect = "Allow"
        Action = ["eks:DescribeCluster"]
        Resource = "arn:aws:eks:${var.aws_region}:${var.account_id}:cluster/${local.name_prefix}-eks"
      }
    ]
  })
}
