output "api_endpoint" {
  value       = aws_apigatewayv2_api.inference.api_endpoint
  description = "Public HTTPS endpoint for the inference API"
}

output "api_id" {
  value = aws_apigatewayv2_api.inference.id
}

output "vpc_link_id" {
  value = aws_apigatewayv2_vpc_link.inference.id
}

output "api_keys_secret_arn" {
  value       = aws_secretsmanager_secret.api_keys.arn
  description = "Secrets Manager ARN holding valid API keys"
}

output "access_log_group_name" {
  value = aws_cloudwatch_log_group.api_access_logs.name
}
