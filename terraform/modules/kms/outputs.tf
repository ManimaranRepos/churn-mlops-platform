output "s3_key_arn" {
  description = "KMS key ARN for S3 bucket encryption"
  value       = aws_kms_key.s3.arn
}

output "s3_key_id" {
  description = "KMS key ID for S3 bucket encryption"
  value       = aws_kms_key.s3.key_id
}

output "rds_key_arn" {
  description = "KMS key ARN for RDS encryption"
  value       = aws_kms_key.rds.arn
}

output "rds_key_id" {
  description = "KMS key ID for RDS encryption"
  value       = aws_kms_key.rds.key_id
}

output "eks_key_arn" {
  description = "KMS key ARN for EKS secrets and EBS encryption"
  value       = aws_kms_key.eks.arn
}

output "eks_key_id" {
  description = "KMS key ID for EKS secrets and EBS encryption"
  value       = aws_kms_key.eks.key_id
}

output "secrets_key_arn" {
  description = "KMS key ARN for Secrets Manager"
  value       = aws_kms_key.secrets.arn
}

output "secrets_key_id" {
  description = "KMS key ID for Secrets Manager"
  value       = aws_kms_key.secrets.key_id
}

output "cloudwatch_key_arn" {
  description = "KMS key ARN for CloudWatch Logs"
  value       = aws_kms_key.cloudwatch.arn
}

output "cloudwatch_key_id" {
  description = "KMS key ID for CloudWatch Logs"
  value       = aws_kms_key.cloudwatch.key_id
}
