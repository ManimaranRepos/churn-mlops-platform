output "kinesis_stream_name" {
  value       = aws_kinesis_stream.events.name
  description = "Kinesis stream name — used by event producer and Lambda"
}

output "kinesis_stream_arn" {
  value = aws_kinesis_stream.events.arn
}

output "firehose_stream_name" {
  value = aws_kinesis_firehose_delivery_stream.events_to_s3.name
}

output "webhook_api_url" {
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/${var.environment}"
  description = "API Gateway URL — configure as webhook endpoint in Segment/Amplitude"
}

output "glue_raw_database" {
  value = aws_glue_catalog_database.raw.name
}

output "glue_curated_database" {
  value = aws_glue_catalog_database.curated.name
}

output "glue_raw_to_curated_job_name" {
  value = aws_glue_job.raw_to_curated.name
}

output "glue_feature_engineering_job_name" {
  value = aws_glue_job.feature_engineering.name
}

output "athena_workgroup" {
  value = aws_athena_workgroup.main.name
}

output "lambda_dlq_url" {
  value       = aws_sqs_queue.lambda_dlq.url
  description = "Monitor this queue for Lambda processing failures"
}
