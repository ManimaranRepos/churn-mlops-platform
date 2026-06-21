# =============================================================================
# ML PLATFORM TERRAFORM — Aurora RDS, ECR, SageMaker resources
# =============================================================================

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# =============================================================================
# AURORA POSTGRESQL — MLflow metadata backend
# =============================================================================
# Why Aurora Serverless v2 instead of provisioned?
# Training runs are bursty: 20 concurrent experiments spike DB load for 30 min,
# then it drops to near-zero. Serverless v2 scales from 0.5 to 4 ACUs in seconds.
# For POC scale, this is ~70% cheaper than provisioned.
# =============================================================================

resource "aws_security_group" "aurora" {
  name        = "${local.name_prefix}-aurora-sg"
  description = "Aurora PostgreSQL for MLflow and Airflow metadata"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from EKS nodes and Glue"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.node_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-aurora-sg" }
}

resource "aws_rds_cluster" "mlflow" {
  cluster_identifier = "${local.name_prefix}-mlflow"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = "15.4"
  database_name      = "mlflow"

  # Read credentials from Secrets Manager (set in Phase 1 security module)
  # manage_master_user_password rotates the password automatically
  manage_master_user_password   = true
  master_username               = "mlflow_admin"
  master_user_secret_kms_key_id = var.kms_rds_key_arn

  db_subnet_group_name   = var.db_subnet_group_name
  vpc_security_group_ids = [aws_security_group.aurora.id]

  storage_encrypted = true
  kms_key_id        = var.kms_rds_key_arn

  # Aurora Serverless v2 scaling: pay only for what you use
  serverlessv2_scaling_configuration {
    min_capacity = var.aurora_min_capacity  # 0.5 ACU ≈ 1GB RAM (idle)
    max_capacity = var.aurora_max_capacity  # 4 ACU ≈ 8GB RAM (training spikes)
  }

  # Automated backups: 7-day retention, taken at 3am UTC
  backup_retention_period      = 7
  preferred_backup_window      = "03:00-04:00"
  preferred_maintenance_window = "sun:04:00-sun:05:00"

  # Protection: prevent accidental deletion
  deletion_protection = var.environment == "prod"

  # Enable CloudWatch logs for slow queries (> 1 second)
  enabled_cloudwatch_logs_exports = ["postgresql"]

  tags = { Name = "${local.name_prefix}-mlflow-aurora" }
}

resource "aws_rds_cluster_instance" "mlflow" {
  # One writer instance for POC. Add read replica for prod with high experiment volume.
  identifier         = "${local.name_prefix}-mlflow-writer"
  cluster_identifier = aws_rds_cluster.mlflow.id
  instance_class     = "db.serverless"  # Required for Serverless v2
  engine             = aws_rds_cluster.mlflow.engine
  engine_version     = aws_rds_cluster.mlflow.engine_version

  # Send PostgreSQL slow query logs to CloudWatch
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = var.kms_rds_key_arn
  performance_insights_retention_period = 7

  tags = { Name = "${local.name_prefix}-mlflow-writer" }
}

# =============================================================================
# ECR REPOSITORIES — Docker images for training containers
# SageMaker pulls training container from ECR when the job starts.
# Custom containers let us pin exact library versions and add our code.
# =============================================================================

locals {
  ecr_repos = {
    mlflow   = "churn-platform/mlflow-server"
    xgboost  = "churn-platform/training-xgboost"
    pytorch  = "churn-platform/training-pytorch"
    inference = "churn-platform/inference-server"
  }
}

resource "aws_ecr_repository" "platform" {
  for_each = local.ecr_repos

  name                 = each.value
  image_tag_mutability = "MUTABLE" # Allow latest tag to be overwritten in dev

  image_scanning_configuration {
    # Scan every pushed image for OS vulnerabilities (CVEs)
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_s3_key_arn
  }

  tags = { Name = each.value }
}

# Lifecycle policy: keep last 10 tagged images, delete untagged after 1 day
resource "aws_ecr_lifecycle_policy" "platform" {
  for_each   = aws_ecr_repository.platform
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged images after 1 day (build cache artifacts)"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      }
    ]
  })
}

# =============================================================================
# SAGEMAKER EXPERIMENT TRACKING
# SageMaker Experiments provides a second layer of tracking alongside MLflow.
# MLflow = our canonical store; SageMaker Experiments = AWS console visibility
# for stakeholders who don't have MLflow access.
# =============================================================================
resource "aws_sagemaker_experiment" "churn_prediction" {
  experiment_name = "${local.name_prefix}-churn-prediction"
  description     = "All training runs for the customer churn prediction model"

  tags = { Name = "${local.name_prefix}-churn-experiment" }
}

# =============================================================================
# SAGEMAKER MODEL REGISTRY
# After training, the best model is registered here.
# Phase 3's ML pipeline transitions it: None → Staging → Production
# =============================================================================
resource "aws_sagemaker_model_package_group" "churn" {
  model_package_group_name        = "${local.name_prefix}-churn-models"
  model_package_group_description = "Versioned churn prediction models for A/B testing and rollback"

  tags = { Name = "${local.name_prefix}-churn-model-registry" }
}

# =============================================================================
# SAGEMAKER ENDPOINT CONFIG — Inference endpoint (referenced in Phase 7)
# Created here so MLflow registration can reference it
# =============================================================================
resource "aws_sagemaker_endpoint_config" "churn" {
  name = "${local.name_prefix}-churn-endpoint-config"

  production_variants {
    variant_name           = "primary"
    initial_instance_count = 1
    instance_type          = "ml.c5.large"
    initial_variant_weight = 1.0
  }

  kms_key_id = var.kms_s3_key_arn

  tags = { Name = "${local.name_prefix}-churn-endpoint-config" }
}

# =============================================================================
# CLOUDWATCH DASHBOARD — Training job metrics at a glance
# =============================================================================
resource "aws_cloudwatch_dashboard" "ml_training" {
  dashboard_name = "${local.name_prefix}-ml-training"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "SageMaker Training Job Status"
          period = 300
          metrics = [
            ["AWS/SageMaker", "TrainingJobsStarted",   "TrainingJobName", "${local.name_prefix}-*"],
            [".", "TrainingJobsCompleted", ".", "."],
            [".", "TrainingJobsFailed",    ".", "."],
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Training GPU/CPU Utilization"
          period = 60
          metrics = [
            ["AWS/SageMaker", "GPUUtilization",    "Host", "${local.name_prefix}-*"],
            [".", "CPUUtilization",    ".", "."],
            [".", "MemoryUtilization", ".", "."],
          ]
        }
      }
    ]
  })
}
