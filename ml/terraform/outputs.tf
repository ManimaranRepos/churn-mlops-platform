output "aurora_endpoint" {
  value       = aws_rds_cluster.mlflow.endpoint
  description = "Aurora writer endpoint — used in MLflow DB connection string"
}

output "aurora_port" {
  value = aws_rds_cluster.mlflow.port
}

output "aurora_database" {
  value = aws_rds_cluster.mlflow.database_name
}

output "aurora_secret_arn" {
  value       = aws_rds_cluster.mlflow.master_user_secret[0].secret_arn
  description = "Secrets Manager ARN for Aurora credentials — mount in MLflow pod via External Secrets"
}

output "ecr_registry" {
  value       = "${var.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
  description = "ECR registry URL prefix"
}

output "ecr_repos" {
  value = { for k, v in aws_ecr_repository.platform : k => v.repository_url }
}

output "sagemaker_experiment_name" {
  value = aws_sagemaker_experiment.churn_prediction.experiment_name
}

output "sagemaker_model_package_group" {
  value = aws_sagemaker_model_package_group.churn.model_package_group_name
}
