output "raw_bucket_name" {
  value = aws_s3_bucket.data_lake["raw"].bucket
}

output "raw_bucket_arn" {
  value = aws_s3_bucket.data_lake["raw"].arn
}

output "processed_bucket_name" {
  value = aws_s3_bucket.data_lake["processed"].bucket
}

output "processed_bucket_arn" {
  value = aws_s3_bucket.data_lake["processed"].arn
}

output "artifacts_bucket_name" {
  value = aws_s3_bucket.data_lake["artifacts"].bucket
}

output "artifacts_bucket_arn" {
  value = aws_s3_bucket.data_lake["artifacts"].arn
}

output "logs_bucket_name" {
  value = aws_s3_bucket.logs.bucket
}

output "logs_bucket_arn" {
  value = aws_s3_bucket.logs.arn
}

output "all_bucket_arns" {
  description = "All bucket ARNs — useful for IAM policy wildcards"
  value = [
    aws_s3_bucket.data_lake["raw"].arn,
    aws_s3_bucket.data_lake["processed"].arn,
    aws_s3_bucket.data_lake["artifacts"].arn,
    aws_s3_bucket.logs.arn
  ]
}
