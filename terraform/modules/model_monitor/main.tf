# =============================================================================
# SageMaker Model Monitor — Terraform resources
#
# Provisions:
#   1. DataQualityJobDefinition — monitors feature distribution vs baseline
#   2. ModelQualityJobDefinition — monitors prediction accuracy vs ground truth
#   3. MonitoringSchedule for each — runs every 6 hours
#   4. Data Capture config (on the SageMaker endpoint, if using SM endpoint path)
#   5. EventBridge rule — triggers drift_detector Lambda when a monitoring job completes
#   6. Retraining trigger Lambda — wraps drift_detector.py logic for Lambda execution
#
# WHY every 6 hours (not hourly)?
#   Monitoring jobs spin up a Processing job cluster (ml.m5.large).
#   Each job takes ~10 min and costs ~$0.03. At 6h intervals: $0.12/day.
#   At 1h intervals: $0.72/day. 6h is an acceptable MTTD for concept drift
#   (which accumulates over days/weeks, not minutes).
#
# WHY EventBridge (not inline Lambda trigger)?
#   SageMaker emits a CloudWatch Event when a monitoring execution completes.
#   EventBridge catches this and invokes the drift detector Lambda.
#   This decouples the monitoring job from the retraining trigger — if the
#   Lambda fails, the monitoring job still completes and we can retry manually.
# =============================================================================

# ── S3 paths ──────────────────────────────────────────────────────────────────
locals {
  baseline_prefix       = "model-monitor/baselines"
  report_prefix         = "model-monitor/reports"
  data_capture_prefix   = "model-monitor/data-capture"
  merged_labels_prefix  = "model-monitor/merged-labels"
}

# ── Data Quality Monitoring ───────────────────────────────────────────────────
resource "aws_sagemaker_data_quality_job_definition" "churn" {
  name     = "${var.project}-${var.environment}-data-quality"
  role_arn = var.sagemaker_role_arn

  data_quality_baseline_config {
    # Baseline produced by baseline_capture.py and stored in S3
    constraints_resource {
      s3_uri = "s3://${var.artifacts_bucket}/${local.baseline_prefix}/${var.model_version}/constraints.json"
    }
    statistics_resource {
      s3_uri = "s3://${var.artifacts_bucket}/${local.baseline_prefix}/${var.model_version}/statistics.json"
    }
  }

  data_quality_app_specification {
    # SageMaker-managed container for data quality monitoring
    image_uri = "156813124566.dkr.ecr.${var.aws_region}.amazonaws.com/sagemaker-model-monitor-analyzer"

    # Check for distribution violations using KS-test (numerical) and chi-squared (categorical)
    environment = {
      "baseline_constraints"     = "s3://${var.artifacts_bucket}/${local.baseline_prefix}/${var.model_version}/constraints.json"
      "baseline_statistics"      = "s3://${var.artifacts_bucket}/${local.baseline_prefix}/${var.model_version}/statistics.json"
      "publish_cloudwatch_metrics" = "Enabled"
    }
  }

  data_quality_job_input {
    # Read from the inference server's captured traffic (our FastAPI server writes here)
    endpoint_input {
      endpoint_name         = var.sagemaker_endpoint_name
      local_path            = "/opt/ml/processing/input/endpoint"
      s3_data_distribution_type = "FullyReplicated"
      s3_input_mode         = "File"
    }
  }

  data_quality_job_output_config {
    monitoring_outputs {
      s3_output {
        local_path    = "/opt/ml/processing/output"
        s3_uri        = "s3://${var.artifacts_bucket}/${local.report_prefix}/data-quality/"
        s3_upload_mode = "EndOfJob"
      }
    }
  }

  job_resources {
    cluster_config {
      instance_count    = 1
      instance_type     = "ml.m5.large"
      volume_size_in_gb = 20
    }
  }

  network_config {
    enable_network_isolation              = false
    enable_inter_container_traffic_encryption = true

    vpc_config {
      security_group_ids = [var.sagemaker_security_group_id]
      subnets            = var.private_subnet_ids
    }
  }

  stopping_condition {
    max_runtime_in_seconds = 1800    # 30 min hard limit
  }

  tags = var.tags
}

resource "aws_sagemaker_monitoring_schedule" "data_quality" {
  name = "${var.project}-${var.environment}-data-quality-schedule"

  monitoring_schedule_config {
    monitoring_job_definition_name = aws_sagemaker_data_quality_job_definition.churn.name
    monitoring_type                = "DataQuality"

    schedule_config {
      # Every 6 hours — aligns with data freshness SLA
      schedule_expression = "cron(0 */6 * * ? *)"
    }
  }

  tags = var.tags
}

# ── Model Quality Monitoring ──────────────────────────────────────────────────
resource "aws_sagemaker_model_quality_job_definition" "churn" {
  name     = "${var.project}-${var.environment}-model-quality"
  role_arn = var.sagemaker_role_arn

  model_quality_baseline_config {
    # Baseline is computed from evaluation metrics on the held-out test set
    # Stored by ml/evaluation/evaluate_model.py after training
    constraints_resource {
      s3_uri = "s3://${var.artifacts_bucket}/${local.baseline_prefix}/${var.model_version}/model_quality_constraints.json"
    }
  }

  model_quality_app_specification {
    image_uri         = "156813124566.dkr.ecr.${var.aws_region}.amazonaws.com/sagemaker-model-monitor-analyzer"
    problem_type      = "BinaryClassification"

    environment = {
      "publish_cloudwatch_metrics" = "Enabled"
    }
  }

  model_quality_job_input {
    # Predictions from Data Capture
    endpoint_input {
      endpoint_name              = var.sagemaker_endpoint_name
      local_path                 = "/opt/ml/processing/input/predictions"
      inference_attribute        = "churn_prediction"
      probability_attribute      = "churn_probability"
      probability_threshold_attribute = tostring(var.model_threshold)
      s3_data_distribution_type  = "FullyReplicated"
      s3_input_mode              = "File"
    }

    # Ground truth labels (written by ground_truth_collector.py)
    ground_truth_s3_input {
      s3_uri = "s3://${var.artifacts_bucket}/${local.merged_labels_prefix}"
    }
  }

  model_quality_job_output_config {
    monitoring_outputs {
      s3_output {
        local_path     = "/opt/ml/processing/output"
        s3_uri         = "s3://${var.artifacts_bucket}/${local.report_prefix}/model-quality/"
        s3_upload_mode = "EndOfJob"
      }
    }
  }

  job_resources {
    cluster_config {
      instance_count    = 1
      instance_type     = "ml.m5.large"
      volume_size_in_gb = 20
    }
  }

  stopping_condition {
    max_runtime_in_seconds = 1800
  }

  tags = var.tags
}

resource "aws_sagemaker_monitoring_schedule" "model_quality" {
  name = "${var.project}-${var.environment}-model-quality-schedule"

  monitoring_schedule_config {
    monitoring_job_definition_name = aws_sagemaker_model_quality_job_definition.churn.name
    monitoring_type                = "ModelQuality"

    schedule_config {
      schedule_expression = "cron(30 */6 * * ? *)"    # Offset 30 min from data quality
    }
  }

  tags = var.tags
}

# ── EventBridge: trigger drift detector when monitoring job completes ─────────
resource "aws_cloudwatch_event_rule" "monitoring_completed" {
  name        = "${var.project}-${var.environment}-monitoring-completed"
  description = "Trigger drift detector when SageMaker monitoring execution completes"

  event_pattern = jsonencode({
    source      = ["aws.sagemaker"]
    detail-type = ["SageMaker Model Monitor Monitoring Execution Status Change"]
    detail = {
      MonitoringScheduleName = [
        aws_sagemaker_monitoring_schedule.data_quality.name,
        aws_sagemaker_monitoring_schedule.model_quality.name,
      ]
      MonitoringExecutionStatus = ["Completed", "CompletedWithViolations"]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "drift_detector_lambda" {
  rule      = aws_cloudwatch_event_rule.monitoring_completed.name
  target_id = "DriftDetectorLambda"
  arn       = aws_lambda_function.drift_detector.arn
}

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.drift_detector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.monitoring_completed.arn
}

# ── Retraining Trigger Lambda ─────────────────────────────────────────────────
resource "aws_lambda_function" "drift_detector" {
  function_name = "${var.project}-${var.environment}-drift-detector"
  role          = aws_iam_role.drift_detector_lambda.arn
  runtime       = "python3.11"
  handler       = "index.handler"
  timeout       = 120
  memory_size   = 256

  filename         = data.archive_file.drift_detector_lambda.output_path
  source_code_hash = data.archive_file.drift_detector_lambda.output_base64sha256

  environment {
    variables = {
      ENVIRONMENT                        = var.environment
      PROJECT                            = var.project
      AWS_REGION_OVERRIDE                = var.aws_region
      ARTIFACTS_BUCKET                   = var.artifacts_bucket
      RAW_BUCKET                         = var.raw_bucket
      AIRFLOW_API_URL                    = var.airflow_api_url
      AIRFLOW_API_SECRET                 = "churn-platform/${var.environment}/airflow-api-credentials"
      DATA_QUALITY_SCHEDULE              = aws_sagemaker_monitoring_schedule.data_quality.name
      MODEL_QUALITY_SCHEDULE             = aws_sagemaker_monitoring_schedule.model_quality.name
      RETRAINING_VIOLATION_THRESHOLD     = "5"
      RETRAINING_DRIFT_SCORE_THRESHOLD   = "0.3"
      SNS_TOPIC_ARN_WARNING              = var.sns_topic_arn_warning
    }
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.drift_detector_lambda.id]
  }

  tags = var.tags
}

data "archive_file" "drift_detector_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/retraining_lambda"
  output_path = "${path.module}/.build/drift_detector.zip"
}

resource "aws_security_group" "drift_detector_lambda" {
  name        = "${var.project}-${var.environment}-drift-detector-sg"
  description = "Drift detector Lambda — outbound to AWS APIs and Airflow"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS to AWS APIs, Airflow"
  }
  egress {
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
    description = "Airflow webserver API"
  }

  tags = merge(var.tags, { Name = "${var.project}-${var.environment}-drift-detector-sg" })
}

resource "aws_iam_role" "drift_detector_lambda" {
  name = "${var.project}-${var.environment}-drift-detector-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "drift_detector_lambda" {
  name = "${var.project}-${var.environment}-drift-detector-policy"
  role = aws_iam_role.drift_detector_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/lambda/${var.project}-${var.environment}-drift-detector:*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.artifacts_bucket}",
          "arn:aws:s3:::${var.artifacts_bucket}/model-monitor/*",
          "arn:aws:s3:::${var.raw_bucket}",
          "arn:aws:s3:::${var.raw_bucket}/ground-truth/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = [
          "sagemaker:ListMonitoringExecutions",
          "sagemaker:DescribeProcessingJob",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "${var.project}/ModelMonitor" }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:churn-platform/${var.environment}/airflow-api-credentials*"
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.sns_topic_arn_warning
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.kms_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"]
        Resource = "*"
      }
    ]
  })
}

# CloudWatch alarms for monitoring health
resource "aws_cloudwatch_metric_alarm" "high_drift_score" {
  alarm_name          = "${var.project}-${var.environment}-model-drift-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "DriftScore"
  namespace           = "${var.project}/ModelMonitor"
  period              = 21600    # 6 hours (matches monitoring schedule frequency)
  statistic           = "Maximum"
  threshold           = 0.3
  alarm_description   = "Model drift score exceeded retraining threshold"
  alarm_actions       = [var.sns_topic_arn_warning]
  ok_actions          = [var.sns_topic_arn_info]

  dimensions = {
    Environment = var.environment
  }

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "model_quality_violations" {
  alarm_name          = "${var.project}-${var.environment}-model-quality-violations"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ViolationCount"
  namespace           = "${var.project}/ModelMonitor"
  period              = 21600
  statistic           = "Maximum"
  threshold           = 3
  alarm_description   = "Model quality monitor detected more than 3 violations"
  alarm_actions       = [var.sns_topic_arn_warning]

  dimensions = {
    Environment    = var.environment
    MonitoringType = "model_quality"
  }

  tags = var.tags
}
