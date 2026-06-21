output "vpc_id" {
  description = "VPC ID — passed to every other module that needs network context"
  value       = aws_vpc.main.id
}

output "vpc_cidr" {
  description = "VPC CIDR block — used in security group rules"
  value       = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  description = "Public subnet IDs — for load balancers and NAT gateways"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Private subnet IDs — for EKS nodes, Lambda, SageMaker"
  value       = aws_subnet.private[*].id
}

output "database_subnet_ids" {
  description = "Database subnet IDs — for RDS Aurora, ElastiCache"
  value       = aws_subnet.database[*].id
}

output "db_subnet_group_name" {
  description = "RDS subnet group name"
  value       = aws_db_subnet_group.main.name
}

output "elasticache_subnet_group_name" {
  description = "ElastiCache subnet group name"
  value       = aws_elasticache_subnet_group.main.name
}

output "nat_gateway_ips" {
  description = "Elastic IPs of NAT gateways — whitelist these in external APIs"
  value       = aws_eip.nat[*].public_ip
}

output "vpc_endpoints_security_group_id" {
  description = "Security group ID for VPC interface endpoints"
  value       = aws_security_group.vpc_endpoints.id
}

output "s3_endpoint_id" {
  description = "S3 Gateway endpoint ID"
  value       = aws_vpc_endpoint.s3.id
}

output "availability_zones" {
  description = "Availability zones used"
  value       = var.availability_zones
}
