output "inference_role_arn" {
  value       = aws_iam_role.inference.arn
  description = "IRSA role ARN — annotate the inference ServiceAccount with this"
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.inference.name
}
