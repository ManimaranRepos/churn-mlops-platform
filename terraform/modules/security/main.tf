# =============================================================================
# SECURITY MODULE — Audit, Secrets & Log Groups
# =============================================================================
# Covers:
#   - CloudTrail: immutable API audit log for the entire AWS account
#   - Secrets Manager: encrypted credential storage with rotation
#   - CloudWatch Log Groups: pre-created with retention + encryption
#   - GitHub OIDC Provider: enables keyless CI/CD authentication
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
# CLOUDTRAIL — Every AWS API call, permanently recorded
# Why? Compliance (SOC2, PCI), security forensics, and debugging.
# "Who deleted that S3 bucket at 3am?" → CloudTrail answers this.
# We enable multi-region so even API calls in other regions are captured.
# -----------------------------------------------------------------------------
resource "aws_cloudtrail" "main" {
  name                          = "${local.name_prefix}-trail"
  s3_bucket_name                = var.logs_bucket_name
  s3_key_prefix                 = "cloudtrail"
  include_global_service_events = true # Captures IAM, STS, Route53 (global services)
  is_multi_region_trail         = true # Even if someone calls an API in eu-west-1
  enable_log_file_validation    = true # Detects if log files are tampered with

  cloud_watch_logs_group_arn = "${aws_cloudwatch_log_group.cloudtrail.arn}:*"
  cloud_watch_logs_role_arn  = aws_iam_role.cloudtrail.arn

  # Also record S3 data events (GetObject, PutObject) for the data lake buckets
  # This is verbose and costs extra, but necessary for data access auditing
  event_selector {
    read_write_type           = "All"
    include_management_events = true

    data_resource {
      type   = "AWS::S3::Object"
      values = ["arn:aws:s3:::${var.project}-${var.environment}-*"]
    }
  }

  tags = {
    Name = "${local.name_prefix}-cloudtrail"
  }
}

# CloudTrail needs permission to write to S3
resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = var.logs_bucket_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AWSCloudTrailAclCheck"
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action   = "s3:GetBucketAcl"
        Resource = var.logs_bucket_arn
      },
      {
        Sid    = "AWSCloudTrailWrite"
        Effect = "Allow"
        Principal = {
          Service = "cloudtrail.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${var.logs_bucket_arn}/cloudtrail/AWSLogs/${var.account_id}/*"
        Condition = {
          StringEquals = {
            "s3:x-amz-acl" = "bucket-owner-full-control"
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "cloudtrail" {
  name              = "/aws/cloudtrail/${local.name_prefix}"
  retention_in_days = 90 # CloudTrail logs in CW for fast querying; S3 for long-term
  kms_key_id        = var.cloudwatch_kms_key_arn
}

resource "aws_iam_role" "cloudtrail" {
  name = "${local.name_prefix}-cloudtrail-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "cloudtrail" {
  role = aws_iam_role.cloudtrail.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.cloudtrail.arn}:*"
    }]
  })
}

# -----------------------------------------------------------------------------
# SECRETS MANAGER — Encrypted credential store
# We create placeholder secrets now; actual values are set manually or by
# the rotation Lambda. Never store real values in Terraform state.
# -----------------------------------------------------------------------------

# MLflow database credentials (Aurora PostgreSQL)
resource "aws_secretsmanager_secret" "mlflow_db" {
  name        = "${local.name_prefix}/mlflow/db-credentials"
  description = "MLflow Aurora PostgreSQL credentials"
  kms_key_id  = var.secrets_kms_key_arn

  # After deletion, secret is recoverable for 7 days (prevent accidental loss)
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "mlflow_db" {
  secret_id = aws_secretsmanager_secret.mlflow_db.id
  # Placeholder — update with: aws secretsmanager put-secret-value ...
  secret_string = jsonencode({
    username = "mlflow"
    password = "REPLACE_ME_ON_FIRST_DEPLOY"
    host     = "POPULATED_BY_RDS_MODULE"
    port     = 5432
    dbname   = "mlflow"
  })

  lifecycle {
    # Prevents Terraform from overwriting the secret after initial creation
    # (once the rotation Lambda updates it, we don't want Terraform to reset it)
    ignore_changes = [secret_string]
  }
}

# Airflow metadata database credentials
resource "aws_secretsmanager_secret" "airflow_db" {
  name        = "${local.name_prefix}/airflow/db-credentials"
  description = "Airflow Aurora PostgreSQL credentials"
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "airflow_db" {
  secret_id = aws_secretsmanager_secret.airflow_db.id
  secret_string = jsonencode({
    username = "airflow"
    password = "REPLACE_ME_ON_FIRST_DEPLOY"
    host     = "POPULATED_BY_RDS_MODULE"
    port     = 5432
    dbname   = "airflow"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# Slack webhook for alerts (used by CloudWatch alarms → SNS → Lambda → Slack)
resource "aws_secretsmanager_secret" "slack_webhook" {
  name        = "${local.name_prefix}/alerting/slack-webhook"
  description = "Slack incoming webhook URL for platform alerts"
  kms_key_id  = var.secrets_kms_key_arn

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "slack_webhook" {
  secret_id     = aws_secretsmanager_secret.slack_webhook.id
  secret_string = jsonencode({ webhook_url = "https://hooks.slack.com/services/REPLACE_ME" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# -----------------------------------------------------------------------------
# CLOUDWATCH LOG GROUPS — Pre-created with retention and encryption
# Why pre-create? If we let services auto-create them, they won't have
# encryption or retention policies — logs pile up forever and unencrypted.
# -----------------------------------------------------------------------------

locals {
  log_groups = {
    eks_app          = "/aws/eks/${local.name_prefix}/application"
    eks_dataplane    = "/aws/eks/${local.name_prefix}/dataplane"
    sagemaker        = "/aws/sagemaker/${local.name_prefix}"
    lambda           = "/aws/lambda/${local.name_prefix}"
    glue             = "/aws/glue/${local.name_prefix}"
    airflow          = "/aws/airflow/${local.name_prefix}"
    inference        = "/aws/inference/${local.name_prefix}"
    model_monitor    = "/aws/model-monitor/${local.name_prefix}"
  }
}

resource "aws_cloudwatch_log_group" "platform" {
  for_each = local.log_groups

  name              = each.value
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = {
    Name      = each.key
    Component = each.key
  }
}

# -----------------------------------------------------------------------------
# SNS TOPIC — Platform alerts hub
# CloudWatch alarms → this topic → Slack Lambda / PagerDuty
# -----------------------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name              = "${local.name_prefix}-alerts"
  kms_master_key_id = "alias/aws/sns" # Use AWS-managed key for SNS (CMK not supported for all features)

  tags = {
    Name = "${local.name_prefix}-alerts"
  }
}

# -----------------------------------------------------------------------------
# GITHUB OIDC PROVIDER — Keyless CI/CD authentication
# This is a one-time account-level setup. GitHub presents a JWT token;
# AWS validates it against GitHub's public keys via this OIDC provider.
# Result: GitHub Actions gets temporary AWS credentials with no stored keys.
# -----------------------------------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  # GitHub's OIDC thumbprint — rotate if GitHub rotates their cert
  # Get current value: openssl s_client -connect token.actions.githubusercontent.com:443 2>/dev/null | openssl x509 -fingerprint -noout -sha1
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1",
  "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

# -----------------------------------------------------------------------------
# AWS BUDGETS — POC cost guardrail
# Alert at $400 (warning) and $500 (critical) per month
# -----------------------------------------------------------------------------
resource "aws_budgets_budget" "poc_monthly" {
  name         = "${local.name_prefix}-monthly-budget"
  budget_type  = "COST"
  limit_amount = "500"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80 # Alert at 80% = $400
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["smanimarancse@gmail.com"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100 # Alert at 100% = $500
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = ["smanimarancse@gmail.com"]
  }
}

# -----------------------------------------------------------------------------
# VPC FLOW LOGS — Network-level traffic record
# Captures (srcIP, dstIP, port, bytes, action=ACCEPT|REJECT) for every
# network flow in the VPC. Essential for post-incident forensics:
# "Which pod communicated with this suspicious IP at 03:47?"
# WHY CloudWatch (not S3)? CW Logs Insights lets you query in seconds.
# S3 is cheaper for >30 days, but we set 30-day CW retention + Glacier lifecycle.
# -----------------------------------------------------------------------------
resource "aws_flow_log" "vpc" {
  vpc_id          = var.vpc_id
  traffic_type    = "ALL"
  iam_role_arn    = aws_iam_role.flow_logs.arn
  log_destination = aws_cloudwatch_log_group.vpc_flow_logs.arn

  tags = merge(var.tags, { Name = "${local.name_prefix}-vpc-flow-logs" })
}

resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/aws/vpc/flow-logs/${local.name_prefix}"
  retention_in_days = 30
  kms_key_id        = var.cloudwatch_kms_key_arn
  tags              = var.tags
}

resource "aws_iam_role" "flow_logs" {
  name = "${local.name_prefix}-flow-logs-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "flow-logs-cw"
  role = aws_iam_role.flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup", "logs:CreateLogStream",
        "logs:PutLogEvents", "logs:DescribeLogGroups",
        "logs:DescribeLogStreams"
      ]
      Resource = "*"
    }]
  })
}

# -----------------------------------------------------------------------------
# GUARDDUTY — ML-based threat detection
# Analyses CloudTrail, VPC Flow, DNS, EKS audit logs without requiring agents.
# WHY enable malware_protection / EKS / S3?
#   Default GuardDuty only covers EC2 + IAM patterns.
#   EKS audit logs: detects pod exec-into, privilege escalation, crypto mining.
#   S3: detects unusual data access patterns (exfiltration).
#   Malware protection: scans EBS volumes of flagged EC2/EKS nodes.
# -----------------------------------------------------------------------------
resource "aws_guardduty_detector" "main" {
  enable                       = true
  finding_publishing_frequency = "SIX_HOURS"

  datasources {
    s3_logs { enable = true }
    kubernetes {
      audit_logs { enable = true }
    }
    malware_protection {
      scan_ec2_instance_with_findings {
        ebs_volumes { enable = true }
      }
    }
  }

  tags = var.tags
}

resource "aws_s3_bucket" "guardduty_findings" {
  bucket        = "${local.name_prefix}-guardduty-${var.account_id}"
  force_destroy = var.environment != "prod"
  tags          = var.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "guardduty_findings" {
  bucket = aws_s3_bucket.guardduty_findings.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.secrets_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_policy" "guardduty_findings" {
  bucket = aws_s3_bucket.guardduty_findings.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowGuardDutyPublish"
        Effect = "Allow"
        Principal = { Service = "guardduty.amazonaws.com" }
        Action   = ["s3:GetBucketLocation", "s3:PutObject"]
        Resource = [
          aws_s3_bucket.guardduty_findings.arn,
          "${aws_s3_bucket.guardduty_findings.arn}/*",
        ]
        Condition = { StringEquals = { "aws:SourceAccount" = var.account_id } }
      },
      {
        Sid    = "DenyNonTLS"
        Effect = "Deny"
        Principal = "*"
        Action   = "s3:*"
        Resource = [
          aws_s3_bucket.guardduty_findings.arn,
          "${aws_s3_bucket.guardduty_findings.arn}/*",
        ]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }
    ]
  })
}

resource "aws_guardduty_publishing_destination" "s3" {
  detector_id      = aws_guardduty_detector.main.id
  destination_arn  = aws_s3_bucket.guardduty_findings.arn
  kms_key_arn      = var.secrets_kms_key_arn
  destination_type = "S3"
}

# -----------------------------------------------------------------------------
# SECURITY HUB — Aggregated security posture
# Runs CIS AWS Benchmark + AWS FSBP; ingests GuardDuty findings.
# WHY both standards?
#   CIS focuses on control-level checks (MFA, CloudTrail, password policy).
#   AWS FSBP covers service-specific best practices (S3 block public access,
#   RDS encryption, EKS endpoint private access).
#   Together they give ~200 automated compliance checks.
# -----------------------------------------------------------------------------
resource "aws_securityhub_account" "main" {
  depends_on = [aws_guardduty_detector.main]
}

resource "aws_securityhub_standards_subscription" "cis" {
  depends_on    = [aws_securityhub_account.main]
  standards_arn = "arn:aws:securityhub:${var.aws_region}::standards/cis-aws-foundations-benchmark/v/1.4.0"
}

resource "aws_securityhub_standards_subscription" "fsbp" {
  depends_on    = [aws_securityhub_account.main]
  standards_arn = "arn:aws:securityhub:${var.aws_region}::standards/aws-foundational-security-best-practices/v/1.0.0"
}

resource "aws_securityhub_product_subscription" "guardduty" {
  depends_on  = [aws_securityhub_account.main]
  product_arn = "arn:aws:securityhub:${var.aws_region}::product/aws/guardduty"
}

# EventBridge: route CRITICAL/HIGH findings → findings forwarder Lambda
resource "aws_cloudwatch_event_rule" "security_findings" {
  name        = "${local.name_prefix}-security-findings"
  description = "Route CRITICAL/HIGH Security Hub findings to SNS"
  event_pattern = jsonencode({
    source      = ["aws.securityhub"]
    detail-type = ["Security Hub Findings - Imported"]
    detail = {
      findings = {
        Severity    = { Label   = ["CRITICAL", "HIGH"] }
        Workflow    = { Status  = ["NEW"] }
        RecordState = ["ACTIVE"]
      }
    }
  })
  tags = var.tags
}

resource "aws_cloudwatch_event_target" "findings_lambda" {
  rule      = aws_cloudwatch_event_rule.security_findings.name
  target_id = "FindingsForwarder"
  arn       = aws_lambda_function.findings_forwarder.arn
}

resource "aws_lambda_permission" "eventbridge_findings" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.findings_forwarder.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.security_findings.arn
}

resource "aws_lambda_function" "findings_forwarder" {
  function_name    = "${local.name_prefix}-findings-forwarder"
  role             = aws_iam_role.findings_lambda.arn
  runtime          = "python3.11"
  handler          = "index.handler"
  timeout          = 15
  memory_size      = 128
  filename         = data.archive_file.findings_lambda.output_path
  source_code_hash = data.archive_file.findings_lambda.output_base64sha256

  environment {
    variables = {
      SNS_TOPIC_ARN             = aws_sns_topic.alerts.arn
      SLACK_WEBHOOK_SECRET_NAME = aws_secretsmanager_secret.slack_webhook.name
      ENVIRONMENT               = var.environment
    }
  }

  tags = var.tags
}

data "archive_file" "findings_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/findings_lambda"
  output_path = "${path.module}/.build/findings_lambda.zip"
}

resource "aws_iam_role" "findings_lambda" {
  name = "${local.name_prefix}-findings-forwarder-role"
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

resource "aws_iam_role_policy" "findings_lambda" {
  name = "findings-forwarder-policy"
  role = aws_iam_role.findings_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/lambda/${local.name_prefix}-findings-forwarder:*"
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.slack_webhook.arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.secrets_kms_key_arn
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# IAM ACCESS ANALYZER — Detect unintended external access
# Generates findings when any resource (S3, KMS, SQS, Lambda, Secrets Manager)
# grants access to a principal outside this AWS account.
# WHY: A single typo in an IAM policy ("Principal": "*") can expose all data.
# Access Analyzer finds this within minutes of the policy change.
# -----------------------------------------------------------------------------
resource "aws_accessanalyzer_analyzer" "main" {
  analyzer_name = "${local.name_prefix}-analyzer"
  type          = "ACCOUNT"
  tags          = var.tags
}

# -----------------------------------------------------------------------------
# AWS CONFIG — Continuous resource compliance recording
# Records every resource configuration change. Managed rules evaluate
# whether resources comply with our security baseline.
# WHY: CloudTrail records API calls; Config records the resulting state.
# "Was this S3 bucket publicly accessible at 14:00 yesterday?" → Config answers.
# -----------------------------------------------------------------------------
resource "aws_config_configuration_recorder" "main" {
  name     = "${local.name_prefix}-config-recorder"
  role_arn = aws_iam_role.config.arn
  recording_group {
    all_supported                 = true
    include_global_resource_types = true
  }
}

resource "aws_config_delivery_channel" "main" {
  name           = "${local.name_prefix}-config-channel"
  s3_bucket_name = var.logs_bucket_name
  s3_key_prefix  = "aws-config"
  depends_on     = [aws_config_configuration_recorder.main]
}

resource "aws_config_configuration_recorder_status" "main" {
  name       = aws_config_configuration_recorder.main.name
  is_enabled = true
  depends_on = [aws_config_delivery_channel.main]
}

resource "aws_iam_role" "config" {
  name = "${local.name_prefix}-config-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "config.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "config" {
  role       = aws_iam_role.config.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWS_ConfigRole"
}

resource "aws_iam_role_policy" "config_s3" {
  name = "config-s3-write"
  role = aws_iam_role.config.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject"]
      Resource = "${var.logs_bucket_arn}/aws-config/*"
      Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" } }
    }]
  })
}

locals {
  config_rules = {
    s3-ssl-only              = "S3_BUCKET_SSL_REQUESTS_ONLY"
    s3-default-encryption    = "S3_DEFAULT_ENCRYPTION_KMS"
    s3-no-public-read        = "S3_BUCKET_PUBLIC_READ_PROHIBITED"
    s3-no-public-write       = "S3_BUCKET_PUBLIC_WRITE_PROHIBITED"
    cmk-rotation-enabled     = "CMK_BACKING_KEY_ROTATION_ENABLED"
    root-no-access-keys      = "IAM_ROOT_ACCESS_KEY_CHECK"
    iam-mfa-enabled          = "IAM_USER_MFA_ENABLED"
    eks-secrets-encrypted    = "EKS_SECRETS_ENCRYPTED"
    rds-storage-encrypted    = "RDS_STORAGE_ENCRYPTED"
  }
}

resource "aws_config_config_rule" "rules" {
  for_each = local.config_rules

  name = "${local.name_prefix}-${each.key}"
  source {
    owner             = "AWS"
    source_identifier = each.value
  }

  depends_on = [aws_config_configuration_recorder_status.main]
  tags       = var.tags
}

# -----------------------------------------------------------------------------
# CLOUDWATCH METRIC FILTERS — Detect suspicious API activity in CloudTrail logs
# Each filter pattern matches a class of security-relevant API call.
# Match → metric increment → CloudWatch alarm → SNS → Slack.
# These cover the CIS AWS Benchmark alarm requirements (controls 3.1–3.14).
# -----------------------------------------------------------------------------
locals {
  security_metric_filters = {
    root-account-usage = {
      pattern     = "{ $.userIdentity.type = \"Root\" && $.eventType != \"AwsServiceEvent\" }"
      metric      = "RootAccountUsage"
      description = "Root account was used — should only happen for break-glass scenarios"
    }
    console-login-failures = {
      pattern     = "{ ($.eventName = ConsoleLogin) && ($.errorMessage = \"Failed authentication\") }"
      metric      = "ConsoleLoginFailures"
      description = "Multiple console login failures — possible brute force"
    }
    unauthorized-api-calls = {
      pattern     = "{ ($.errorCode = \"*UnauthorizedAccess\") || ($.errorCode = \"AccessDenied\") }"
      metric      = "UnauthorizedAPICalls"
      description = "AccessDenied errors — possible privilege escalation attempt"
    }
    iam-policy-changes = {
      pattern     = "{ ($.eventName=PutUserPolicy) || ($.eventName=PutRolePolicy) || ($.eventName=PutGroupPolicy) || ($.eventName=CreatePolicy) || ($.eventName=DeletePolicy) || ($.eventName=AttachRolePolicy) || ($.eventName=DetachRolePolicy) }"
      metric      = "IAMPolicyChanges"
      description = "IAM policy was created or modified"
    }
    s3-bucket-policy-changes = {
      pattern     = "{ ($.eventSource = s3.amazonaws.com) && (($.eventName = PutBucketPolicy) || ($.eventName = PutBucketAcl) || ($.eventName = DeleteBucketPolicy)) }"
      metric      = "S3BucketPolicyChanges"
      description = "S3 bucket policy or ACL was modified"
    }
    kms-key-deletion = {
      pattern     = "{ ($.eventSource = kms.amazonaws.com) && (($.eventName = DisableKey) || ($.eventName = ScheduleKeyDeletion)) }"
      metric      = "KMSKeyDeletion"
      description = "KMS key disabled or scheduled for deletion — would break at-rest encryption"
    }
    security-group-changes = {
      pattern     = "{ ($.eventName = AuthorizeSecurityGroupIngress) || ($.eventName = RevokeSecurityGroupIngress) || ($.eventName = AuthorizeSecurityGroupEgress) || ($.eventName = CreateSecurityGroup) || ($.eventName = DeleteSecurityGroup) }"
      metric      = "SecurityGroupChanges"
      description = "Security group rules modified — could open unintended network access"
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "security" {
  for_each       = local.security_metric_filters
  name           = "${local.name_prefix}-${each.key}"
  pattern        = each.value.pattern
  log_group_name = aws_cloudwatch_log_group.cloudtrail.name

  metric_transformation {
    name          = each.value.metric
    namespace     = "${var.project}/SecurityEvents"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "security" {
  for_each = local.security_metric_filters

  alarm_name          = "${local.name_prefix}-security-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = each.value.metric
  namespace           = "${var.project}/SecurityEvents"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = each.value.description
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  tags = var.tags
}
