# =============================================================================
# Phase 6 — Airflow Infrastructure
#
# WHY Airflow over alternatives?
#   - Dagster: excellent UI but requires persistent daemon pods (higher baseline cost)
#   - Prefect: managed control plane (data leaves VPC for scheduling)
#   - Step Functions: no Python DAG syntax; harder to test locally; expensive at high task counts
#   - Airflow on EKS with KubernetesExecutor: tasks run as pods (scale to zero),
#     DAGs are code, local development uses LocalExecutor, same codebase everywhere.
#
# Resources created here:
#   - IRSA role for Airflow pods (Glue, SageMaker, S3, Athena, Kinesis)
#   - Fernet key in Secrets Manager (encrypts sensitive DAG variables at rest)
#   - Airflow DB credentials secret (Aurora PostgreSQL — same cluster as MLflow)
#   - CloudWatch log group for Airflow task logs
#   - SQS queue for DAG-triggered alerts (feeds into SNS in Phase 8)
#
# NOT created here (already exist from earlier phases):
#   - Aurora PostgreSQL cluster (ml/terraform/main.tf)
#   - S3 logs bucket (terraform/modules/s3)
#   - VPC + EKS (terraform/modules/vpc + eks)
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.name
  name_prefix = "${var.project}-${var.environment}"
  tags = {
    Environment = var.environment
    Project     = var.project
    Team        = "ml-platform"
    CostCenter  = "orchestration"
    ManagedBy   = "terraform"
    Component   = "airflow"
  }
}

# ── Fernet Key — encrypts passwords/tokens stored in Airflow DB ───────────────
# WHY Secrets Manager (not SSM Parameter Store)?
#   Fernet key needs 32-byte base64 value and auto-rotation capability.
#   Secrets Manager has built-in rotation support (SSM doesn't for custom secrets).
resource "aws_secretsmanager_secret" "airflow_fernet_key" {
  name                    = "${local.name_prefix}/airflow/fernet-key"
  description             = "Airflow Fernet key — encrypts sensitive DB values"
  kms_key_id              = var.kms_key_arn_secrets
  recovery_window_in_days = 7

  tags = local.tags
}

# The actual key value must be set manually after apply:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Then: aws secretsmanager put-secret-value --secret-id ... --secret-string '{"fernet_key": "<value>"}'
resource "aws_secretsmanager_secret_version" "airflow_fernet_key" {
  secret_id     = aws_secretsmanager_secret.airflow_fernet_key.id
  secret_string = jsonencode({ fernet_key = "PLACEHOLDER_SET_MANUALLY_BEFORE_DEPLOY" })

  lifecycle {
    ignore_changes = [secret_string]  # Don't overwrite after manual/rotated value is set
  }
}

# ── Airflow DB credentials ─────────────────────────────────────────────────────
resource "aws_secretsmanager_secret" "airflow_db" {
  name                    = "${local.name_prefix}/airflow/db-credentials"
  description             = "Airflow Aurora PostgreSQL connection"
  kms_key_id              = var.kms_key_arn_secrets
  recovery_window_in_days = 7
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "airflow_db" {
  secret_id = aws_secretsmanager_secret.airflow_db.id
  secret_string = jsonencode({
    username = "airflow"
    password = "PLACEHOLDER_SET_BY_RDS_ROTATION"
    host     = var.aurora_host
    port     = "5432"
    dbname   = "airflow"
  })
  lifecycle {
    ignore_changes = [secret_string]
  }
}

# ── Airflow Git SSH key — for gitSync to pull private DAG repo ────────────────
resource "aws_secretsmanager_secret" "airflow_git_ssh_key" {
  name                    = "${local.name_prefix}/airflow/git-ssh-key"
  description             = "SSH private key for Airflow gitSync to pull DAG repo"
  kms_key_id              = var.kms_key_arn_secrets
  recovery_window_in_days = 7
  tags                    = local.tags
}

# ── IRSA Role — Airflow scheduler + workers ───────────────────────────────────
# WHY separate role for Airflow (not reusing SageMaker role)?
#   Least privilege: Airflow needs to READ from S3, SUBMIT SageMaker jobs,
#   TRIGGER Glue jobs, and READ Athena. It does NOT need to write ML artifacts.
#   Separate role means a compromised DAG cannot overwrite model artifacts.
data "aws_iam_policy_document" "airflow_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.eks_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(var.eks_oidc_provider_url, "https://", "")}:sub"
      values   = ["system:serviceaccount:airflow:airflow-worker"]
    }
    condition {
      test     = "StringEquals"
      variable = "${replace(var.eks_oidc_provider_url, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "airflow" {
  name               = "${local.name_prefix}-airflow-irsa"
  assume_role_policy = data.aws_iam_policy_document.airflow_assume_role.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "airflow_permissions" {
  name = "airflow-permissions"
  role = aws_iam_role.airflow.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3: read raw + processed, write to processed (for train/val/test splits)
      {
        Sid    = "S3DataAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
          "s3:ListBucket", "s3:GetBucketLocation"
        ]
        Resource = [
          "arn:aws:s3:::${var.raw_bucket}",
          "arn:aws:s3:::${var.raw_bucket}/*",
          "arn:aws:s3:::${var.processed_bucket}",
          "arn:aws:s3:::${var.processed_bucket}/*",
          "arn:aws:s3:::${var.artifacts_bucket}/airflow-logs/*",
        ]
      },
      # Glue: start/stop crawlers and jobs (for feature engineering)
      {
        Sid    = "GlueAccess"
        Effect = "Allow"
        Action = [
          "glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns",
          "glue:StartCrawler", "glue:GetCrawler", "glue:GetCrawlerMetrics",
          "glue:GetTable", "glue:GetTables", "glue:GetDatabase",
        ]
        Resource = [
          "arn:aws:glue:${local.region}:${local.account_id}:catalog",
          "arn:aws:glue:${local.region}:${local.account_id}:database/*",
          "arn:aws:glue:${local.region}:${local.account_id}:table/*",
          "arn:aws:glue:${local.region}:${local.account_id}:job/*",
          "arn:aws:glue:${local.region}:${local.account_id}:crawler/*",
        ]
      },
      # Athena: run queries for data validation
      {
        Sid    = "AthenaAccess"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution", "athena:GetQueryExecution",
          "athena:GetQueryResults", "athena:StopQueryExecution",
          "athena:GetWorkGroup",
        ]
        Resource = [
          "arn:aws:athena:${local.region}:${local.account_id}:workgroup/*",
        ]
      },
      # SageMaker: submit training jobs, check status, register models
      {
        Sid    = "SageMakerTraining"
        Effect = "Allow"
        Action = [
          "sagemaker:CreateTrainingJob",
          "sagemaker:DescribeTrainingJob",
          "sagemaker:StopTrainingJob",
          "sagemaker:CreateHyperParameterTuningJob",
          "sagemaker:DescribeHyperParameterTuningJob",
          "sagemaker:ListTrainingJobsForHyperParameterTuningJob",
        ]
        Resource = [
          "arn:aws:sagemaker:${local.region}:${local.account_id}:training-job/*",
          "arn:aws:sagemaker:${local.region}:${local.account_id}:hyper-parameter-tuning-job/*",
        ]
      },
      # IAM PassRole: needed when submitting SageMaker jobs (passes SageMaker exec role)
      {
        Sid    = "PassRoleForSageMaker"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [var.sagemaker_execution_role_arn]
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "sagemaker.amazonaws.com"
          }
        }
      },
      # CloudWatch: emit custom metrics from DAG tasks
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = ["cloudwatch:PutMetricData"]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "ChurnPlatform/Airflow"
          }
        }
      },
      # Secrets Manager: read Airflow own secrets + MLflow/Slack credentials
      {
        Sid    = "ReadOwnSecrets"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.airflow_fernet_key.arn,
          aws_secretsmanager_secret.airflow_db.arn,
          "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:${local.name_prefix}/slack/*",
        ]
      },
      # KMS: decrypt secrets
      {
        Sid    = "KMSDecrypt"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.kms_key_arn_secrets, var.kms_key_arn_s3]
      },
      # Kinesis: read stream metrics (for data quality DAG)
      {
        Sid    = "KinesisRead"
        Effect = "Allow"
        Action = [
          "kinesis:GetShardIterator", "kinesis:GetRecords",
          "kinesis:DescribeStream", "kinesis:ListShards",
        ]
        Resource = ["arn:aws:kinesis:${local.region}:${local.account_id}:stream/${var.kinesis_stream_name}"]
      },
    ]
  })
}

# ── CloudWatch log group for Airflow task logs ────────────────────────────────
resource "aws_cloudwatch_log_group" "airflow" {
  name              = "/aws/eks/churn-platform/${var.environment}/airflow"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn_cloudwatch
  tags              = local.tags
}

# ── SQS for DAG completion events (fed into Phase 8 alerting) ─────────────────
resource "aws_sqs_queue" "dag_events" {
  name                       = "${local.name_prefix}-dag-events"
  message_retention_seconds  = 86400  # 1 day
  visibility_timeout_seconds = 300
  kms_master_key_id          = var.kms_key_arn_secrets
  tags                       = local.tags
}

resource "aws_sqs_queue_policy" "dag_events" {
  queue_url = aws_sqs_queue.dag_events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.airflow.arn }
      Action    = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
      Resource  = aws_sqs_queue.dag_events.arn
    }]
  })
}
