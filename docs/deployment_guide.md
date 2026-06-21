# Deployment Guide — From Zero to Production

This guide walks through deploying the Churn Platform from scratch into a greenfield AWS account.
Estimated time: 3–4 hours for the first deploy. Subsequent environment deploys: ~45 minutes.

## Prerequisites

Local tools required:
```bash
# Verify all required tools are installed
terraform version          # >= 1.6
aws --version              # >= 2.0
kubectl version --client   # >= 1.28
helm version               # >= 3.12
argocd version             # >= 2.9
python3 --version          # >= 3.11
```

AWS account requirements:
- A greenfield AWS account (or one with no conflicting resources)
- An IAM user or role with AdministratorAccess for the initial bootstrap
- A GitHub repository forked from this repo

---

## Phase 0 — Bootstrap (one-time, ~20 min)

### 0.1 Configure AWS credentials
```bash
aws configure
# AWS Access Key ID: <your-key>
# AWS Secret Access Key: <your-secret>
# Default region: us-east-1
# Default output format: json

# Verify
aws sts get-caller-identity
```

### 0.2 Create Terraform state backend
```bash
# Create S3 bucket for Terraform state (manually, before Terraform runs)
aws s3api create-bucket \
  --bucket churn-platform-terraform-state-$(aws sts get-caller-identity --query Account --output text) \
  --region us-east-1

aws s3api put-bucket-versioning \
  --bucket churn-platform-terraform-state-<account-id> \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket churn-platform-terraform-state-<account-id> \
  --server-side-encryption-configuration '{
    "Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]
  }'

# Create DynamoDB table for state locking
aws dynamodb create-table \
  --table-name churn-platform-terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

Update `terraform/backend.tf` with the bucket name and account ID.

### 0.3 Set GitHub secrets
In your GitHub repository → Settings → Secrets and variables → Actions:

| Secret | Value |
|--------|-------|
| `AWS_ACCOUNT_ID` | Your 12-digit account ID |
| `AWS_REGION` | `us-east-1` |
| `ECR_REGISTRY` | `<account>.dkr.ecr.us-east-1.amazonaws.com` |
| `ARTIFACTS_BUCKET` | `churn-platform-dev-artifacts-<account>` |
| `RAW_BUCKET` | `churn-platform-dev-raw-<account>` |
| `PROCESSED_BUCKET` | `churn-platform-dev-processed-<account>` |
| `SLACK_WEBHOOK_URL` | Your Slack incoming webhook URL |

---

## Phase 1 — Core Infrastructure (~45 min)

```bash
cd terraform/

# Initialise Terraform
terraform init

# Select dev workspace
terraform workspace new dev
terraform workspace select dev

# Plan first — review what will be created
terraform plan -out=tfplan

# Apply (creates VPC, EKS, Aurora, ECR, KMS, S3 buckets, IAM roles)
terraform apply tfplan
```

Key outputs to note (save these):
```bash
terraform output eks_cluster_name          # e.g. churn-platform-dev
terraform output eks_oidc_provider_arn     # for IRSA
terraform output aurora_endpoint           # for Airflow + MLflow
terraform output artifacts_bucket          # for SageMaker + MLflow
```

### Configure kubectl
```bash
aws eks update-kubeconfig \
  --name $(terraform output -raw eks_cluster_name) \
  --region us-east-1

kubectl get nodes  # Should show EKS worker nodes
```

---

## Phase 2 — Platform Services (~30 min)

### Install ArgoCD
```bash
kubectl create namespace argocd
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.9.0/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=Available deployment/argocd-server -n argocd --timeout=5m

# Get initial admin password
argocd admin initial-password -n argocd

# Login
kubectl port-forward svc/argocd-server -n argocd 8443:443 &
argocd login localhost:8443 --username admin --insecure
```

### Install External Secrets Operator
```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --set installCRDs=true
```

### Install OPA Gatekeeper (via ArgoCD)
```bash
# Update the repoURL in argocd/apps/gatekeeper.yaml with your GitHub repo
sed -i 's|YOUR_ORG|<your-github-org>|g' argocd/apps/gatekeeper.yaml

argocd app create -f argocd/apps/gatekeeper.yaml
argocd app sync gatekeeper
argocd app wait gatekeeper --health --timeout 300
```

### Configure ClusterSecretStore (External Secrets → Secrets Manager)
```bash
# Apply the ClusterSecretStore (uses EKS Pod Identity / IRSA)
kubectl apply -f k8s/external-secrets/cluster-secret-store.yaml

# Verify it's ready
kubectl get clustersecretstore aws-secrets-manager
```

---

## Phase 3 — Application Deployments (~20 min)

Update all `YOUR_ORG` placeholders in ArgoCD apps:
```bash
find argocd/ -name "*.yaml" -exec \
  sed -i 's|YOUR_ORG|<your-github-org>|g' {} \;
git add argocd/ && git commit -m "configure github org" && git push
```

Deploy all platform applications:
```bash
# Deploy in order (Gatekeeper must be ready before workload apps)
for app in mlflow airflow churn-inference monitoring; do
  argocd app create -f argocd/apps/${app}.yaml 2>/dev/null || true
  argocd app sync ${app}
done

# Wait for all to be healthy
argocd app list
```

Expected: all apps show `Synced / Healthy` within 10 minutes.

---

## Phase 4 — Secrets Population (~10 min)

Terraform creates secret placeholders with `REPLACE_ME` values. Populate them:

```bash
# Airflow Fernet key (generate a new one)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

aws secretsmanager put-secret-value \
  --secret-id churn-platform/airflow/fernet-key \
  --secret-string '{"fernet_key":"<generated-key>"}'

# Slack webhook
aws secretsmanager put-secret-value \
  --secret-id churn-platform/dev/slack-webhook \
  --secret-string '{"webhook_url":"https://hooks.slack.com/services/<your-webhook>"}'

# API keys for inference API
aws secretsmanager put-secret-value \
  --secret-id churn-platform/dev/api-keys \
  --secret-string '{
    "crm_system": "<generate-random-32-char-string>",
    "ml_pipeline": "<generate-random-32-char-string>"
  }'

# Git SSH key for Airflow gitSync (if repo is private)
aws secretsmanager put-secret-value \
  --secret-id churn-platform/airflow/git-ssh-key \
  --secret-string "$(cat ~/.ssh/id_ed25519 | python3 -c "import sys,json; print(json.dumps({'ssh_key': sys.stdin.read()}))")"
```

---

## Phase 5 — First Pipeline Run (~30 min)

### Seed test data (dev only)
```bash
# Generate synthetic customer events for testing
python3 scripts/generate_test_data.py \
  --num-customers 10000 \
  --churn-rate 0.10 \
  --output-bucket ${RAW_BUCKET} \
  --partition $(date +%Y/%m/%d)
```

### Trigger the feature pipeline
```bash
# Port-forward Airflow webserver
kubectl port-forward -n airflow svc/airflow-webserver 8080:8080 &

# Unpause and trigger the feature pipeline
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags unpause churn_feature_pipeline

kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags trigger churn_feature_pipeline

# Monitor progress
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags list-runs -d churn_feature_pipeline --limit 3
```

Expected: `churn_feature_pipeline` completes in ~40 minutes and triggers `churn_training_pipeline`.

### Monitor training
```bash
# SageMaker training jobs
aws sagemaker list-training-jobs \
  --name-contains churn \
  --sort-by CreationTime \
  --sort-order Descending \
  --query 'TrainingJobSummaries[].{Name:TrainingJobName,Status:TrainingJobStatus}'
```

---

## Phase 6 — Validate the platform

```bash
# 1. Test the inference API
API_ENDPOINT=$(terraform output -raw api_gateway_endpoint)
API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id churn-platform/dev/api-keys \
  --query SecretString --output text | python3 -c "import json,sys; print(json.load(sys.stdin)['ml_pipeline'])")

curl -X POST ${API_ENDPOINT}/predict \
  -H "X-Api-Key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "VALIDATE_001",
    "features": {
      "customer_tenure_months": 24,
      "monthly_charges": 79.95,
      "contract_type": "Month-to-month",
      "payment_method": "Electronic check",
      "support_tickets_90d": 3,
      "total_charges": 1918.80
    }
  }'

# Expected: {"churn_probability": <float>, "churn_prediction": <bool>, "cached": false, ...}

# 2. Verify monitoring is scraping metrics
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 &
curl -s http://localhost:9090/api/v1/query?query=churn_api_requests_total | python3 -m json.tool

# 3. Check Grafana dashboard is loading
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80 &
# Open http://localhost:3000 — default admin password from Secrets Manager

# 4. Confirm GuardDuty is enabled
aws guardduty list-detectors --query 'DetectorIds[0]' --output text

# 5. Confirm Security Hub has findings (some expected from new account)
aws securityhub get-findings \
  --filters '{"WorkflowStatus":[{"Value":"NEW","Comparison":"EQUALS"}]}' \
  --query 'length(Findings)'
```

---

## Staging and Production deploys

After validating dev:

```bash
# Staging
terraform workspace new staging
terraform apply -var-file=envs/staging.tfvars

# Production (requires manual approval in GitHub Actions)
terraform workspace new prod
terraform apply -var-file=envs/prod.tfvars
```

Prod differences (in `envs/prod.tfvars`):
- `min_capacity = 1` (Aurora never scales to 0)
- `inference_min_replicas = 3` (one per AZ)
- `spot_instances_enabled = false` (on-demand only for serving)
- `enable_deletion_protection = true` (prevents accidental Aurora/ElastiCache deletion)

---

## Rollback a Terraform change

```bash
# See what changed in the last apply
terraform show -json terraform.tfstate | python3 -m json.tool | head -100

# Revert to a specific state version (from S3 versioning)
aws s3api list-object-versions \
  --bucket churn-platform-terraform-state-<account> \
  --prefix dev/terraform.tfstate \
  --query 'Versions[].{VersionId:VersionId,LastModified:LastModified}' | head -20

# Download and restore a previous state
aws s3api get-object \
  --bucket churn-platform-terraform-state-<account> \
  --key dev/terraform.tfstate \
  --version-id <version-id> \
  terraform.tfstate.backup

terraform apply  # Will reconcile to match the restored state
```
