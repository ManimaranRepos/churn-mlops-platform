# =============================================================================
# Monitoring Terraform module
#
# Provisions the AWS-side monitoring infrastructure:
#   1. SNS topics (critical/warning/info) — AlertManager sends alerts here
#   2. Lambda Slack forwarder — consumes SNS, posts to Slack channels
#   3. CloudWatch dashboards — AWS-native metrics (API GW, ElastiCache, Kinesis)
#   4. CloudWatch Log Insights saved queries — for fast incident investigation
#   5. Grafana IRSA role — grants Grafana pods permission to query CloudWatch
#
# The kube-prometheus-stack itself is deployed via ArgoCD (argocd/apps/monitoring.yaml).
# Terraform only manages the AWS resources that Prometheus/Grafana depend on.
# =============================================================================

# ── SNS Topics ────────────────────────────────────────────────────────────────
# Three topics match the three AlertManager receivers (critical/warning/info).
# The alertmanager-sns-forwarder sidecar (running alongside AlertManager) publishes
# to these topics. The forwarder uses IRSA credentials to publish — no access key needed.

resource "aws_sns_topic" "alerts_critical" {
  name              = "${var.project}-${var.environment}-alerts-critical"
  kms_master_key_id = var.kms_key_arn

  tags = merge(var.tags, { AlertSeverity = "critical" })
}

resource "aws_sns_topic" "alerts_warning" {
  name              = "${var.project}-${var.environment}-alerts-warning"
  kms_master_key_id = var.kms_key_arn

  tags = merge(var.tags, { AlertSeverity = "warning" })
}

resource "aws_sns_topic" "alerts_info" {
  name              = "${var.project}-${var.environment}-alerts-info"
  kms_master_key_id = var.kms_key_arn

  tags = merge(var.tags, { AlertSeverity = "info" })
}

# ── SNS Topic Policy ─────────────────────────────────────────────────────────
# Allow AlertManager pod (via IRSA role) to publish to these topics.
# Also allow CloudWatch alarms to publish directly (for AWS-side alerts).
locals {
  sns_arns = [
    aws_sns_topic.alerts_critical.arn,
    aws_sns_topic.alerts_warning.arn,
    aws_sns_topic.alerts_info.arn,
  ]
}

resource "aws_sns_topic_policy" "alerts" {
  for_each = {
    critical = aws_sns_topic.alerts_critical.arn
    warning  = aws_sns_topic.alerts_warning.arn
    info     = aws_sns_topic.alerts_info.arn
  }
  arn = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAlertManagerPublish"
        Effect = "Allow"
        Principal = { AWS = var.alertmanager_irsa_role_arn }
        Action   = "SNS:Publish"
        Resource = each.value
      },
      {
        Sid    = "AllowCloudWatchAlarms"
        Effect = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action   = "SNS:Publish"
        Resource = each.value
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:cloudwatch:${var.aws_region}:${var.account_id}:alarm:${var.project}-${var.environment}-*"
          }
        }
      }
    ]
  })
}

# ── Slack Webhook Secret ──────────────────────────────────────────────────────
resource "aws_secretsmanager_secret" "slack_webhook" {
  name                    = "churn-platform/${var.environment}/slack-webhook"
  description             = "Slack incoming webhook URL for alert notifications"
  recovery_window_in_days = 0

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "slack_webhook" {
  secret_id     = aws_secretsmanager_secret.slack_webhook.id
  secret_string = jsonencode({
    webhook_url = "REPLACE_WITH_SLACK_WEBHOOK_URL"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# ── Slack Forwarder Lambda ────────────────────────────────────────────────────
resource "aws_lambda_function" "slack_forwarder" {
  function_name = "${var.project}-${var.environment}-slack-forwarder"
  role          = aws_iam_role.slack_lambda.arn
  runtime       = "python3.11"
  handler       = "index.handler"
  timeout       = 15
  memory_size   = 128

  filename         = data.archive_file.slack_lambda.output_path
  source_code_hash = data.archive_file.slack_lambda.output_base64sha256

  environment {
    variables = {
      SLACK_WEBHOOK_SECRET_NAME = aws_secretsmanager_secret.slack_webhook.name
      SLACK_CHANNEL_CRITICAL    = var.slack_channel_critical
      SLACK_CHANNEL_WARNING     = var.slack_channel_warning
      SLACK_CHANNEL_INFO        = var.slack_channel_info
      AWS_REGION_OVERRIDE       = var.aws_region
    }
  }

  # VPC placement: Lambda must reach Secrets Manager (via VPC endpoint or NAT)
  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.slack_lambda.id]
  }

  tags = var.tags
}

data "archive_file" "slack_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/slack_lambda"
  output_path = "${path.module}/.build/slack_lambda.zip"
}

resource "aws_security_group" "slack_lambda" {
  name        = "${var.project}-${var.environment}-slack-lambda-sg"
  description = "Slack forwarder Lambda — outbound to Secrets Manager and Slack HTTPS"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS outbound (Slack API + Secrets Manager)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.project}-${var.environment}-slack-lambda-sg" })
}

resource "aws_iam_role" "slack_lambda" {
  name = "${var.project}-${var.environment}-slack-lambda-role"

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

resource "aws_iam_role_policy" "slack_lambda" {
  name = "${var.project}-${var.environment}-slack-lambda-policy"
  role = aws_iam_role.slack_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/lambda/${var.project}-${var.environment}-slack-forwarder:*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.slack_webhook.arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.kms_key_arn
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DeleteNetworkInterface"
        ]
        Resource = "*"
      }
    ]
  })
}

# Subscribe Lambda to all three SNS topics
resource "aws_sns_topic_subscription" "slack_critical" {
  topic_arn = aws_sns_topic.alerts_critical.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_forwarder.arn
}

resource "aws_sns_topic_subscription" "slack_warning" {
  topic_arn = aws_sns_topic.alerts_warning.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_forwarder.arn
}

resource "aws_sns_topic_subscription" "slack_info" {
  topic_arn = aws_sns_topic.alerts_info.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.slack_forwarder.arn
}

resource "aws_lambda_permission" "sns_critical" {
  statement_id  = "AllowSNSCritical"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_forwarder.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts_critical.arn
}

resource "aws_lambda_permission" "sns_warning" {
  statement_id  = "AllowSNSWarning"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_forwarder.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts_warning.arn
}

resource "aws_lambda_permission" "sns_info" {
  statement_id  = "AllowSNSInfo"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_forwarder.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alerts_info.arn
}

# ── CloudWatch Dashboard — AWS-native metrics ─────────────────────────────────
# Grafana covers Prometheus metrics. This CW dashboard covers:
#   - API Gateway (not scraped by Prometheus — CW only)
#   - ElastiCache Redis (not on a /metrics endpoint)
#   - Kinesis shards and throttles
#   - SageMaker training job status
resource "aws_cloudwatch_dashboard" "churn_platform" {
  dashboard_name = "${var.project}-${var.environment}-platform"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x = 0; y = 0; width = 24; height = 1
        properties = {
          markdown = "## Churn Platform — AWS Infrastructure | ${var.environment} | [Grafana](${var.grafana_url})"
        }
      },

      # API Gateway
      {
        type = "metric"; x = 0; y = 1; width = 8; height = 6
        properties = {
          title  = "API Gateway — Request Count"
          metrics = [["AWS/ApiGateway", "Count", "ApiId", var.api_gateway_id, "Stage", "$default"]]
          period = 60; stat = "Sum"; view = "timeSeries"
          region = var.aws_region
        }
      },
      {
        type = "metric"; x = 8; y = 1; width = 8; height = 6
        properties = {
          title  = "API Gateway — P99 Latency"
          metrics = [["AWS/ApiGateway", "IntegrationLatency", "ApiId", var.api_gateway_id, "Stage", "$default", { "stat" = "p99" }]]
          period = 60; view = "timeSeries"
          region = var.aws_region
          annotations = { horizontal = [{ value = 200, label = "Quality Gate 200ms", color = "#ff7f0e" }] }
        }
      },
      {
        type = "metric"; x = 16; y = 1; width = 8; height = 6
        properties = {
          title  = "API Gateway — 5xx Errors"
          metrics = [["AWS/ApiGateway", "5XXError", "ApiId", var.api_gateway_id, "Stage", "$default"]]
          period = 60; stat = "Sum"; view = "timeSeries"
          region = var.aws_region
        }
      },

      # ElastiCache
      {
        type = "metric"; x = 0; y = 7; width = 8; height = 6
        properties = {
          title   = "Redis — Memory Usage %"
          metrics = [["AWS/ElastiCache", "DatabaseMemoryUsagePercentage", "ReplicationGroupId", var.elasticache_cluster_id]]
          period  = 300; stat = "Average"; view = "timeSeries"
          region  = var.aws_region
          annotations = { horizontal = [{ value = 80, label = "80% threshold", color = "#ff7f0e" }] }
        }
      },
      {
        type = "metric"; x = 8; y = 7; width = 8; height = 6
        properties = {
          title   = "Redis — Cache Hits vs Misses"
          metrics = [
            ["AWS/ElastiCache", "CacheHits",   "ReplicationGroupId", var.elasticache_cluster_id],
            ["AWS/ElastiCache", "CacheMisses",  "ReplicationGroupId", var.elasticache_cluster_id]
          ]
          period = 300; stat = "Sum"; view = "timeSeries"
          region = var.aws_region
        }
      },
      {
        type = "metric"; x = 16; y = 7; width = 8; height = 6
        properties = {
          title   = "Redis — Evictions"
          metrics = [["AWS/ElastiCache", "Evictions", "ReplicationGroupId", var.elasticache_cluster_id]]
          period  = 300; stat = "Sum"; view = "timeSeries"
          region  = var.aws_region
        }
      },

      # Kinesis
      {
        type = "metric"; x = 0; y = 13; width = 12; height = 6
        properties = {
          title   = "Kinesis — Incoming Records"
          metrics = [["AWS/Kinesis", "IncomingRecords", "StreamName", var.kinesis_stream_name]]
          period  = 900; stat = "Sum"; view = "timeSeries"
          region  = var.aws_region
        }
      },
      {
        type = "metric"; x = 12; y = 13; width = 12; height = 6
        properties = {
          title   = "Kinesis — Iterator Age (consumers lagging)"
          metrics = [["AWS/Kinesis", "GetRecords.IteratorAgeMilliseconds", "StreamName", var.kinesis_stream_name]]
          period  = 900; stat = "Maximum"; view = "timeSeries"
          region  = var.aws_region
          annotations = { horizontal = [{ value = 300000, label = "5 min threshold", color = "#ff7f0e" }] }
        }
      }
    ]
  })
}

# ── CloudWatch Log Insights Saved Queries ─────────────────────────────────────
# Saved queries appear in the CloudWatch console under "Logs Insights → Saved queries".
# Engineers use these during incidents to quickly answer standard questions.

resource "aws_cloudwatch_query_definition" "inference_errors" {
  name = "${var.project}/${var.environment}/inference-errors-last-hour"

  log_group_names = [
    "/aws/eks/${var.project}-${var.environment}/inference"
  ]

  query_string = <<-EOQ
    fields @timestamp, @message, customer_id, status_code, error
    | filter status_code >= 400
    | sort @timestamp desc
    | limit 100
  EOQ
}

resource "aws_cloudwatch_query_definition" "slow_predictions" {
  name = "${var.project}/${var.environment}/slow-predictions-p99"

  log_group_names = [
    "/aws/eks/${var.project}-${var.environment}/inference"
  ]

  query_string = <<-EOQ
    fields @timestamp, customer_id, latency_ms, cached, model_type
    | filter latency_ms > 200
    | stats
        count() as count,
        avg(latency_ms) as avg_ms,
        pct(latency_ms, 99) as p99_ms
      by bin(5m)
    | sort @timestamp desc
  EOQ
}

resource "aws_cloudwatch_query_definition" "api_gateway_4xx" {
  name = "${var.project}/${var.environment}/api-gateway-auth-failures"

  log_group_names = [
    "/aws/apigateway/${var.project}-${var.environment}-inference"
  ]

  query_string = <<-EOQ
    fields @timestamp, routeKey, status, ip, apiKeyId
    | filter status = 403 or status = 401
    | stats count() as auth_failures by ip, apiKeyId
    | sort auth_failures desc
    | limit 20
  EOQ
}

resource "aws_cloudwatch_query_definition" "pipeline_failures" {
  name = "${var.project}/${var.environment}/airflow-pipeline-failures"

  log_group_names = [
    "/aws/eks/${var.project}-${var.environment}/airflow"
  ]

  query_string = <<-EOQ
    fields @timestamp, @message, dag_id, task_id, run_id
    | filter @message like /ERROR/ or @message like /FAILED/
    | filter dag_id like /churn/
    | sort @timestamp desc
    | limit 50
  EOQ
}

# ── Grafana IRSA Role ─────────────────────────────────────────────────────────
# Grafana pods need CloudWatch:GetMetricData to display the CloudWatch datasource
resource "aws_iam_role" "grafana" {
  name = "${var.project}-${var.environment}-grafana-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.eks_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.eks_oidc_provider_url}:sub" = "system:serviceaccount:monitoring:kube-prometheus-stack-grafana"
          "${var.eks_oidc_provider_url}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "grafana" {
  name = "${var.project}-${var.environment}-grafana-policy"
  role = aws_iam_role.grafana.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchRead"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarmsForMetric",
          "cloudwatch:DescribeAlarmHistory",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:ListMetrics",
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:GetMetricData",
          "cloudwatch:GetInsightRuleReport"
        ]
        Resource = "*"
      },
      {
        Sid    = "LogsRead"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:GetLogGroupFields",
          "logs:StartQuery",
          "logs:StopQuery",
          "logs:GetQueryResults",
          "logs:GetLogEvents"
        ]
        Resource = "*"
      },
      {
        Sid    = "TagsRead"
        Effect = "Allow"
        Action = ["tag:GetResources"]
        Resource = "*"
      }
    ]
  })
}
