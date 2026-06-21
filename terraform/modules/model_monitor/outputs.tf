output "data_quality_schedule_name" {
  value = aws_sagemaker_monitoring_schedule.data_quality.name
}

output "model_quality_schedule_name" {
  value = aws_sagemaker_monitoring_schedule.model_quality.name
}

output "drift_detector_lambda_arn" {
  value = aws_lambda_function.drift_detector.arn
}

output "drift_detector_lambda_name" {
  value = aws_lambda_function.drift_detector.function_name
}

output "data_quality_job_definition_name" {
  value = aws_sagemaker_data_quality_job_definition.churn.name
}

output "model_quality_job_definition_name" {
  value = aws_sagemaker_model_quality_job_definition.churn.name
}
