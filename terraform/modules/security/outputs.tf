output "cloudtrail_arn" {
  value = aws_cloudtrail.main.arn
}

output "alerts_sns_topic_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "SNS topic ARN — subscribe Lambda/PagerDuty here for alerts"
}

output "mlflow_db_secret_arn" {
  value = aws_secretsmanager_secret.mlflow_db.arn
}

output "airflow_db_secret_arn" {
  value = aws_secretsmanager_secret.airflow_db.arn
}

output "slack_webhook_secret_arn" {
  value = aws_secretsmanager_secret.slack_webhook.arn
}

output "github_oidc_provider_arn" {
  value       = aws_iam_openid_connect_provider.github.arn
  description = "Used by the IAM module to create the CI/CD role trust policy"
}

output "cloudwatch_log_group_arns" {
  value = { for k, v in aws_cloudwatch_log_group.platform : k => v.arn }
}

output "guardduty_detector_id" {
  value = aws_guardduty_detector.main.id
}

output "config_recorder_name" {
  value = aws_config_configuration_recorder.main.name
}

output "access_analyzer_arn" {
  value = aws_accessanalyzer_analyzer.main.arn
}

output "vpc_flow_log_group_name" {
  value = aws_cloudwatch_log_group.vpc_flow_logs.name
}

output "findings_forwarder_lambda_arn" {
  value = aws_lambda_function.findings_forwarder.arn
}
