# =============================================================================
# ElastiCache Redis — prediction result cache
#
# WHY ElastiCache (not self-managed Redis on EKS)?
#   - Managed failover: if the primary fails, ElastiCache promotes the replica
#     in ~30s. Self-managed requires Redis Sentinel or Cluster mode setup.
#   - Automated backups to S3 (daily snapshots, 1-day retention for cache).
#   - Parameter group management: Redis config changes without SSH access.
#   - CloudWatch metrics built-in: CacheHits, CacheMisses, Evictions, Memory.
#
# WHY Redis (not Memcached)?
#   - Supports TTL per key (critical: predictions expire, Memcached TTL is per-item
#     but with no guaranteed eviction ordering).
#   - Supports scan_iter for pattern-based invalidation (used in cache.py).
#   - AUTH + TLS in transit supported (Memcached does not support AUTH).
#
# Cluster mode: DISABLED (cluster_mode_enabled = false)
#   - We don't need horizontal sharding — prediction cache is small (<1GB).
#   - Cluster mode requires hash-slot-aware clients; simpler clients work with
#     replication group (primary + replica) mode.
#   - One primary + one replica across two AZs = HA without cluster complexity.
# =============================================================================

resource "aws_elasticache_subnet_group" "redis" {
  name        = "${var.project}-${var.environment}-redis-subnet-group"
  description = "ElastiCache subnet group for inference prediction cache"
  subnet_ids  = var.private_subnet_ids

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-redis-subnet-group"
  })
}

resource "aws_security_group" "redis" {
  name        = "${var.project}-${var.environment}-redis-sg"
  description = "ElastiCache Redis — allow inbound from EKS worker nodes only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Redis from EKS worker nodes"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [var.eks_worker_security_group_id]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-redis-sg"
  })
}

resource "aws_elasticache_parameter_group" "redis" {
  name        = "${var.project}-${var.environment}-redis-params"
  family      = "redis7"
  description = "Churn platform Redis parameter group"

  # WHY maxmemory-policy = allkeys-lru?
  #   When Redis memory is full, we want it to evict the least-recently-used keys
  #   (old predictions) rather than returning OOM errors. Cache data is safe to evict
  #   — the worst case is a cache miss, not data loss.
  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }

  # Disable slow log for latency (saves a small amount of overhead at high QPS)
  parameter {
    name  = "slowlog-log-slower-than"
    value = "10000"    # 10ms — only log truly slow commands
  }

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-redis-params"
  })
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${var.project}-${var.environment}-redis"
  description          = "Churn prediction cache — primary + 1 replica across AZs"

  node_type            = var.node_type        # default: cache.t4g.small (2 vCPU, 1.37GB, Graviton2)
  num_cache_clusters   = 2                    # 1 primary + 1 replica
  port                 = 6379
  parameter_group_name = aws_elasticache_parameter_group.redis.name
  subnet_group_name    = aws_elasticache_subnet_group.redis.name
  security_group_ids   = [aws_security_group.redis.id]

  # Redis version — 7.x for improved ACL, faster I/O threads
  engine_version = "7.1"

  # Encryption
  at_rest_encryption_enabled  = true
  transit_encryption_enabled  = true    # Requires auth_token or TLS-capable client
  auth_token                  = random_password.redis_auth_token.result

  # Failover: promote replica when primary fails (takes ~30s)
  automatic_failover_enabled  = true
  multi_az_enabled            = true

  # Maintenance and backup
  maintenance_window         = "sun:05:00-sun:06:00"   # UTC — low traffic window
  snapshot_retention_limit   = 1                        # 1 day for cache (not critical data)
  snapshot_window            = "04:00-05:00"
  apply_immediately          = var.environment != "prod"   # In prod, wait for maintenance window

  # Notifications → SNS → Phase 8 alerting
  notification_topic_arn = var.sns_topic_arn

  tags = merge(var.tags, {
    Name = "${var.project}-${var.environment}-redis"
  })
}

# Auth token stored in Secrets Manager — referenced by ESO in Helm chart
resource "random_password" "redis_auth_token" {
  length           = 32
  special          = false    # Redis AUTH token: only printable non-special ASCII
  override_special = ""
}

resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "churn-platform/elasticache/redis-url"
  description             = "ElastiCache Redis connection URL for inference pods"
  recovery_window_in_days = var.environment == "prod" ? 30 : 0

  tags = var.tags
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id = aws_secretsmanager_secret.redis_url.id
  secret_string = jsonencode({
    url = "rediss://:${random_password.redis_auth_token.result}@${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
  })

  lifecycle {
    # Never overwrite the secret once set — allow rotation without Terraform destroying it
    ignore_changes = [secret_string]
  }
}

# CloudWatch alarms for Redis health (feed into Phase 8 Grafana dashboard)
resource "aws_cloudwatch_metric_alarm" "redis_high_memory" {
  alarm_name          = "${var.project}-${var.environment}-redis-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "DatabaseMemoryUsagePercentage"
  namespace           = "AWS/ElastiCache"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "Redis memory > 80% — consider scaling up node type"
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.redis.id
  }

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "redis_evictions" {
  alarm_name          = "${var.project}-${var.environment}-redis-evictions"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Evictions"
  namespace           = "AWS/ElastiCache"
  period              = 300
  statistic           = "Sum"
  threshold           = 1000   # More than 1000 evictions in 5 min → memory pressure
  alarm_description   = "Redis eviction rate high — cache thrashing"
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.redis.id
  }

  tags = var.tags
}
