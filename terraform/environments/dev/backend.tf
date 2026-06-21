# =============================================================================
# REMOTE STATE BACKEND
# =============================================================================
# This file is generated after running terraform/bootstrap.
# Replace ACCOUNT_ID with your actual AWS account ID.
# State key is environment-scoped so dev/staging/prod don't share state.
# =============================================================================
terraform {
  backend "s3" {
    bucket         = "churn-platform-terraform-state-ACCOUNT_ID"
    key            = "dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "churn-platform-terraform-locks"
    encrypt        = true
  }
}
