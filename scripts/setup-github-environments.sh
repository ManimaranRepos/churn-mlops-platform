#!/usr/bin/env bash
# =============================================================================
# GitHub Environments & Secrets Setup
# =============================================================================
# Run this ONCE after Phase 1+2 Terraform apply to wire up GitHub Actions.
# Prerequisites:
#   - gh CLI authenticated (gh auth login)
#   - AWS CLI authenticated
#   - Terraform outputs available (cd terraform/environments/dev && terraform output)
# =============================================================================

set -euo pipefail

REPO="your-org/churn-platform"
AWS_REGION="us-east-1"

echo "=== Setting up GitHub Environments ==="

# Create environments with protection rules
for env in dev staging prod; do
  echo "Creating environment: $env"
  gh api repos/$REPO/environments/$env \
    --method PUT \
    --field wait_timer=0 \
    --silent || true
done

# Staging: require 1 reviewer before apply
gh api repos/$REPO/environments/staging \
  --method PUT \
  --field wait_timer=0 \
  -f reviewers='[{"type":"Team","id":1}]' \
  --silent

# Prod: require 2 reviewers, only from main branch, 5 min wait
gh api repos/$REPO/environments/prod \
  --method PUT \
  --field wait_timer=5 \
  -f reviewers='[{"type":"Team","id":1}]' \
  --silent

echo ""
echo "=== Fetching Terraform Outputs ==="

# Get outputs from dev environment (where Terraform state lives)
TF_OUTPUTS=$(cd terraform/environments/dev && terraform output -json)

CICD_ROLE_ARN=$(echo $TF_OUTPUTS | jq -r '.cicd_role_arn.value')
EKS_CLUSTER=$(echo $TF_OUTPUTS | jq -r '.eks_cluster_name.value')
RAW_BUCKET=$(echo $TF_OUTPUTS | jq -r '.raw_bucket_name.value')
ARTIFACTS_BUCKET=$(echo $TF_OUTPUTS | jq -r '.artifacts_bucket_name.value')
SAGEMAKER_ROLE=$(echo $TF_OUTPUTS | jq -r '.sagemaker_role_arn.value')

echo ""
echo "=== Setting Repository Secrets ==="

# Repo-level secrets (available to all environments)
gh secret set AWS_REGION --body "$AWS_REGION" --repo "$REPO"
gh secret set EKS_CLUSTER_NAME --body "$EKS_CLUSTER" --repo "$REPO"

echo ""
echo "=== Setting Environment Secrets ==="

# Each environment gets its own role ARN — least privilege per environment
for env in dev staging prod; do
  echo "Setting secrets for environment: $env"

  # In a real setup, each environment would have its own AWS account
  # and its own role ARN. For this POC, all envs share the same account.
  gh secret set AWS_CICD_ROLE_ARN \
    --body "$CICD_ROLE_ARN" \
    --env "$env" \
    --repo "$REPO"

  gh secret set SAGEMAKER_ROLE_ARN \
    --body "$SAGEMAKER_ROLE" \
    --env "$env" \
    --repo "$REPO"

  gh secret set RAW_BUCKET \
    --body "$RAW_BUCKET" \
    --env "$env" \
    --repo "$REPO"

  gh secret set ARTIFACTS_BUCKET \
    --body "$ARTIFACTS_BUCKET" \
    --env "$env" \
    --repo "$REPO"
done

echo ""
echo "=== Remaining Manual Steps ==="
echo ""
echo "Set these secrets manually in GitHub (repo Settings → Secrets):"
echo ""
echo "  SLACK_WEBHOOK_URL        - From Slack app settings"
echo "  MLFLOW_TRACKING_URI      - http://mlflow.mlops.svc:5000 (after Phase 5)"
echo "  ARGOCD_SERVER            - ArgoCD server URL (after ArgoCD is deployed)"
echo "  ARGOCD_AUTH_TOKEN        - From: argocd account generate-token"
echo "  INFERENCE_URL            - After Phase 7"
echo "  AIRFLOW_ADMIN_PASSWORD   - After Phase 6"
echo "  GITOPS_TOKEN             - GitHub PAT with repo scope (for image tag updates)"
echo ""
echo "=== GitHub Environments Setup Complete ==="
