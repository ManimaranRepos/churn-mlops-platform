# =============================================================================
# VPC MODULE — Network Foundation
# =============================================================================
# Three-tier network: public (load balancers) → private (compute) →
# database (RDS, isolated). Only public tier has IGW route. Private uses
# NAT for outbound. Database has NO outbound route at all.
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  name_prefix = "${var.project}-${var.environment}"

  # How many NAT gateways to create:
  # prod: one per AZ (3) = high availability
  # dev: one total = cost saving (~$100/month difference)
  nat_gateway_count = var.single_nat_gateway ? 1 : length(var.availability_zones)
}

# -----------------------------------------------------------------------------
# VPC
# -----------------------------------------------------------------------------
resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  # Required for EKS — pods need to resolve internal AWS service DNS names
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${local.name_prefix}-vpc"
    # EKS needs these tags to discover the VPC for cluster networking
    "kubernetes.io/cluster/${local.name_prefix}-eks" = "shared"
  }
}

# -----------------------------------------------------------------------------
# Internet Gateway — Entry/exit point for public subnets
# -----------------------------------------------------------------------------
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

# -----------------------------------------------------------------------------
# Public Subnets — Load balancers, NAT Gateway EIPs live here
# -----------------------------------------------------------------------------
resource "aws_subnet" "public" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.public_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  # Instances launched here get a public IP automatically
  # Needed for load balancers to receive traffic
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name_prefix}-public-${var.availability_zones[count.index]}"
    Tier = "public"
    # AWS Load Balancer Controller discovers subnets via these tags
    "kubernetes.io/role/elb"                         = "1"
    "kubernetes.io/cluster/${local.name_prefix}-eks" = "shared"
  }
}

# -----------------------------------------------------------------------------
# Private Subnets — EKS nodes, application pods
# -----------------------------------------------------------------------------
resource "aws_subnet" "private" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  # No public IPs — these instances are never directly reachable from internet
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-private-${var.availability_zones[count.index]}"
    Tier = "private"
    # internal-elb tag: internal ALBs for pod-to-pod cross-namespace traffic
    "kubernetes.io/role/internal-elb"                = "1"
    "kubernetes.io/cluster/${local.name_prefix}-eks" = "shared"
    # Karpenter discovers subnets with this tag when provisioning new nodes
    "karpenter.sh/discovery" = "${local.name_prefix}-eks"
  }
}

# -----------------------------------------------------------------------------
# Database Subnets — RDS Aurora, ElastiCache Redis
# Isolated: no internet route whatsoever (not even NAT)
# -----------------------------------------------------------------------------
resource "aws_subnet" "database" {
  count             = length(var.availability_zones)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.database_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-database-${var.availability_zones[count.index]}"
    Tier = "database"
  }
}

# RDS requires a "subnet group" — a named collection of subnets it can use
resource "aws_db_subnet_group" "main" {
  name        = "${local.name_prefix}-db-subnet-group"
  subnet_ids  = aws_subnet.database[*].id
  description = "Subnet group for RDS Aurora clusters in ${var.environment}"

  tags = {
    Name = "${local.name_prefix}-db-subnet-group"
  }
}

# ElastiCache also needs its own subnet group type
resource "aws_elasticache_subnet_group" "main" {
  name        = "${local.name_prefix}-cache-subnet-group"
  subnet_ids  = aws_subnet.database[*].id
  description = "Subnet group for ElastiCache Redis in ${var.environment}"
}

# -----------------------------------------------------------------------------
# Elastic IPs for NAT Gateways
# NAT Gateway needs a static public IP so outbound traffic has a consistent
# source IP (useful for IP-whitelisting external APIs)
# -----------------------------------------------------------------------------
resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? local.nat_gateway_count : 0
  domain = "vpc"

  tags = {
    Name = "${local.name_prefix}-nat-eip-${count.index + 1}"
  }

  # IGW must exist before EIPs can be associated
  depends_on = [aws_internet_gateway.main]
}

# -----------------------------------------------------------------------------
# NAT Gateways — One per AZ (prod) or one total (dev)
# Placed in PUBLIC subnets so they can reach the internet
# Private subnet route tables point to these for outbound traffic
# -----------------------------------------------------------------------------
resource "aws_nat_gateway" "main" {
  count         = var.enable_nat_gateway ? local.nat_gateway_count : 0
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name = "${local.name_prefix}-nat-${var.availability_zones[count.index]}"
  }

  depends_on = [aws_internet_gateway.main]
}

# -----------------------------------------------------------------------------
# Route Tables
# -----------------------------------------------------------------------------

# Public route table: 0.0.0.0/0 → Internet Gateway
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name_prefix}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  count          = length(var.availability_zones)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private route tables: one per AZ, pointing to the NAT in the same AZ
# Why one per AZ? If the NAT in AZ-a fails, AZ-b traffic shouldn't route through it
resource "aws_route_table" "private" {
  count  = length(var.availability_zones)
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-private-rt-${var.availability_zones[count.index]}"
  }
}

resource "aws_route" "private_nat" {
  count = var.enable_nat_gateway ? length(var.availability_zones) : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  # In single_nat_gateway mode, all AZs point to the same NAT (index 0)
  nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.main[0].id : aws_nat_gateway.main[count.index].id
}

resource "aws_route_table_association" "private" {
  count          = length(var.availability_zones)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Database route table: NO default route — databases cannot initiate outbound connections
resource "aws_route_table" "database" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-database-rt"
  }
}

resource "aws_route_table_association" "database" {
  count          = length(var.availability_zones)
  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = aws_route_table.database.id
}

# -----------------------------------------------------------------------------
# VPC Flow Logs — Capture all IP traffic metadata
# Not packet contents (that would be too much data) — just src/dst IP,
# port, protocol, bytes, and accept/reject. Essential for security audits.
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/aws/vpc/flow-logs/${local.name_prefix}"
  retention_in_days = var.flow_logs_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = {
    Name = "${local.name_prefix}-vpc-flow-logs"
  }
}

resource "aws_iam_role" "vpc_flow_logs" {
  name = "${local.name_prefix}-vpc-flow-logs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "vpc-flow-logs.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name = "${local.name_prefix}-vpc-flow-logs-policy"
  role = aws_iam_role.vpc_flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams"
      ]
      Resource = "*"
    }]
  })
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL" # Capture both ACCEPT and REJECT — rejects reveal attack patterns
  iam_role_arn    = aws_iam_role.vpc_flow_logs.arn
  log_destination = aws_cloudwatch_log_group.vpc_flow_logs.arn

  tags = {
    Name = "${local.name_prefix}-flow-log"
  }
}

# -----------------------------------------------------------------------------
# VPC Endpoints — Keep AWS service traffic inside the AWS backbone
# Without these, S3/ECR/STS calls from private subnets go:
#   private subnet → NAT gateway → internet → S3
# With endpoints, they go:
#   private subnet → VPC endpoint → S3 (never leaves AWS network)
# Benefits: faster, cheaper (no NAT data charges), and more secure
# -----------------------------------------------------------------------------

# S3 Gateway Endpoint — free, no per-hour charge, handles S3 traffic
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(aws_route_table.private[*].id, [aws_route_table.database.id])

  tags = {
    Name = "${local.name_prefix}-s3-endpoint"
  }
}

# ECR endpoints — EKS nodes pull Docker images from ECR.
# Without this, every image pull goes through NAT = expensive + slow.
resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name_prefix}-ecr-dkr-endpoint"
  }
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name_prefix}-ecr-api-endpoint"
  }
}

# STS endpoint — required for IAM role assumption from pods (IRSA / pod identity)
resource "aws_vpc_endpoint" "sts" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.sts"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name_prefix}-sts-endpoint"
  }
}

# Secrets Manager endpoint — Lambda and pods fetch secrets without going to internet
resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name_prefix}-secretsmanager-endpoint"
  }
}

# CloudWatch endpoint — metrics and logs from private subnets
resource "aws_vpc_endpoint" "cloudwatch_logs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name_prefix}-cloudwatch-logs-endpoint"
  }
}

# -----------------------------------------------------------------------------
# Security Group for VPC Endpoints
# Only allow HTTPS (443) from within the VPC — endpoints don't need anything else
# -----------------------------------------------------------------------------
resource "aws_security_group" "vpc_endpoints" {
  name        = "${local.name_prefix}-vpc-endpoints-sg"
  description = "Security group for VPC interface endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-vpc-endpoints-sg"
  }
}
