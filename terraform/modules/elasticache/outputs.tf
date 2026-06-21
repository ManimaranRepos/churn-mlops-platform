output "primary_endpoint" {
  value       = aws_elasticache_replication_group.redis.primary_endpoint_address
  description = "Redis primary endpoint (write)"
}

output "reader_endpoint" {
  value       = aws_elasticache_replication_group.redis.reader_endpoint_address
  description = "Redis reader endpoint (read-only replica)"
}

output "redis_url_secret_arn" {
  value       = aws_secretsmanager_secret.redis_url.arn
  description = "Secrets Manager ARN for the Redis connection URL (used by ESO)"
}

output "security_group_id" {
  value       = aws_security_group.redis.id
}
