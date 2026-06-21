output "sns_topic_arn_critical" {
  value = aws_sns_topic.alerts_critical.arn
}

output "sns_topic_arn_warning" {
  value = aws_sns_topic.alerts_warning.arn
}

output "sns_topic_arn_info" {
  value = aws_sns_topic.alerts_info.arn
}

output "slack_forwarder_lambda_arn" {
  value = aws_lambda_function.slack_forwarder.arn
}

output "grafana_irsa_role_arn" {
  value       = aws_iam_role.grafana.arn
  description = "Annotate kube-prometheus-stack-grafana ServiceAccount with this ARN"
}

output "slack_webhook_secret_arn" {
  value = aws_secretsmanager_secret.slack_webhook.arn
}

output "cloudwatch_dashboard_url" {
  value = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.churn_platform.dashboard_name}"
}
