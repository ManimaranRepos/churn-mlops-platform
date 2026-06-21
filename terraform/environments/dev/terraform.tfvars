# Dev environment — cost-optimized settings
# Key differences from prod:
#   - single_nat_gateway = true   → saves ~$100/month (loses AZ redundancy for outbound)
#   - shorter log retention       → saves storage costs
#   - smaller CIDR ranges are fine since fewer services run here

aws_region  = "us-east-1"
environment = "dev"
project     = "churn-platform"
