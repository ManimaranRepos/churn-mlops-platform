# Churn MLOps Platform - Production Deployment Guide

## Prerequisites

- **AWS Account** with appropriate permissions (EC2, EKS, S3, IAM, KMS, RDS)
- **AWS CLI** configured: `aws configure`
- **Terraform** ≥ 1.6: `brew install terraform` (macOS) or see [terraform.io](https://www.terraform.io/downloads.html)
- **kubectl** ≥ 1.27: `brew install kubectl`
- **Helm** 3: `brew install helm`
- **Docker**: [docker.com](https://www.docker.com)
- **Python** 3.11+: `python3 --version`

## Phase 1: Bootstrap (30 minutes)

### 1.1 Prepare Terraform State Backend

Terraform needs a remote backend to store state. We'll create an S3 bucket + DynamoDB table:

```bash
cd terraform/bootstrap

# Review what will be created
terraform init
terraform plan

# Apply (one-time)
terraform apply -auto-approve

# Note the S3 bucket name and DynamoDB table name from output
cd ..
```

### 1.2 Create GitHub Secrets (for CI/CD)

1. Go to GitHub repo → Settings → Secrets and variables → Actions
2. Add these secrets:
   ```
   AWS_ACCESS_KEY_ID = your-key-id
   AWS_SECRET_ACCESS_KEY = your-secret-key
   AWS_REGION = us-east-1
   ```

## Phase 2: Deploy Infrastructure (60-90 minutes)

### 2.1 Deploy to Dev Environment

```bash
cd terraform/environments/dev

# Initialize Terraform with remote backend
terraform init

# Validate configuration
terraform validate

# Review what will be created
terraform plan -out=tfplan

# Apply changes (this takes ~60-90 minutes)
terraform apply tfplan
```

**What gets created:**
- VPC with public/private subnets
- EKS cluster (3 nodes, t3.large)
- RDS PostgreSQL (for MLflow, Airflow)
- S3 buckets (data lake, artifacts, logging)
- IAM roles and policies
- Security groups
- KMS encryption keys

### 2.2 Configure kubectl

```bash
# Get EKS cluster credentials
aws eks update-kubeconfig \
  --region us-east-1 \
  --name churn-mlops-dev-eks

# Verify connection
kubectl get nodes
# Should show 3 nodes in Ready state
```

## Phase 3: Deploy Platform Services (30-45 minutes)

### 3.1 Bootstrap ArgoCD

```bash
# Create argocd namespace and install
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=available --timeout=300s \
  deployment/argocd-server -n argocd

# Get ArgoCD admin password
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d

# Port-forward to access UI
kubectl port-forward svc/argocd-server -n argocd 8080:443
# Access: https://localhost:8080 (username: admin, password from above)
```

### 3.2 Deploy App-of-Apps (GitOps)

```bash
# Apply the ArgoCD bootstrap config
kubectl apply -f argocd/bootstrap/app-of-apps.yaml

# Monitor deployment
kubectl get applications -n argocd -w

# Check pod status
kubectl get pods -A
```

**Applications being deployed:**
- Cert-manager (SSL/TLS)
- Karpenter (auto-scaler)
- ALB Ingress Controller
- Prometheus (monitoring)
- Grafana (dashboards)
- MLflow (experiment tracking)
- Airflow (orchestration)
- FastAPI inference service

### 3.3 Verify All Services

```bash
# Check all pods are running
kubectl get pods -A --field-selector=status.phase!=Running
# Should return no results

# Check services
kubectl get svc -A

# Check ingresses
kubectl get ingress -A
```

## Phase 4: Configure Data Pipeline (15-30 minutes)

### 4.1 Create S3 Data Buckets

```bash
# The Terraform already created these, but verify:
aws s3 ls | grep churn-mlops-dev

# You should see:
# churn-mlops-dev-raw-data
# churn-mlops-dev-curated-data
# churn-mlops-dev-artifacts
```

### 4.2 Upload Sample Data

```bash
# Generate sample data
python ml/training/generate_sample_data.py --output data/sample.csv

# Upload to S3
aws s3 cp data/sample.csv s3://churn-mlops-dev-raw-data/2024-07/
```

### 4.3 Configure Airflow DAGs

```bash
# Get Airflow service URL
kubectl get svc -n airflow

# Port-forward to Airflow UI
kubectl port-forward svc/airflow-webserver -n airflow 8081:8080
# Access: http://localhost:8081

# Create Airflow connections for AWS
# In Airflow UI → Admin → Connections → Create:
# Connection ID: aws_default
# Connection Type: Amazon Web Services
# Extra: {"region_name": "us-east-1"}
```

## Phase 5: Train First Model (30-60 minutes)

### 5.1 Trigger Training Pipeline

```bash
# Option 1: Via Airflow UI
# Navigate to training_pipeline DAG → Trigger DAG

# Option 2: Via CLI
kubectl exec -it deployment/airflow-webserver -n airflow -- \
  airflow dags trigger training_pipeline

# Monitor progress
kubectl logs -f deployment/airflow-webserver -n airflow
```

### 5.2 Monitor in MLflow

```bash
# Access MLflow UI
kubectl port-forward svc/mlflow -n mlflow 5000:5000
# Go to http://localhost:5000

# You should see:
# - Experiments created
# - Training runs with metrics (accuracy, AUC, etc.)
# - Best model registered in Model Registry
```

## Phase 6: Deploy Inference Service (15-30 minutes)

### 6.1 Deploy FastAPI Service

Already deployed by ArgoCD, but verify:

```bash
# Check FastAPI pods
kubectl get pods -n inference

# Get service URL
kubectl get svc -n inference

# Port-forward to test
kubectl port-forward svc/fastapi-service -n inference 8000:8000
```

### 6.2 Test Inference Endpoint

```bash
# Health check
curl http://localhost:8000/health

# Prediction
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{
    "customer_age": 35,
    "monthly_charges": 75.5,
    "contract_length": 24,
    "tech_support": true
  }'

# Expected response:
# {
#   "churn_probability": 0.23,
#   "predicted_churn": false,
#   "confidence": 0.92
# }
```

## Phase 7: Setup Monitoring (15-30 minutes)

### 7.1 Access Grafana Dashboards

```bash
# Port-forward to Grafana
kubectl port-forward svc/grafana -n monitoring 3000:80

# Access: http://localhost:3000
# Default credentials: admin / admin (change on first login)
```

### 7.2 Configure Alerts

```bash
# Apply Prometheus alert rules
kubectl apply -f monitoring/prometheus/rules.yaml -n monitoring

# Alerts will trigger if:
# - Inference latency > 500ms
# - Model accuracy drops > 5%
# - Data drift detected
# - Pod memory usage > 80%
```

## Phase 8: Setup CI/CD (10-15 minutes)

### 8.1 GitHub Actions Workflow

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy to Dev

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v2
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: us-east-1
      
      - name: Build and push Docker image
        run: |
          aws ecr get-login-password --region us-east-1 | \
            docker login --username AWS --password-stdin ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com
          
          docker build -t inference:${{ github.sha }} .
          docker tag inference:${{ github.sha }} \
            ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com/churn-mlops:${{ github.sha }}
          docker push \
            ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com/churn-mlops:${{ github.sha }}
      
      - name: Update Helm values
        run: |
          sed -i "s|image: .*|image: ${{ secrets.AWS_ACCOUNT_ID }}.dkr.ecr.us-east-1.amazonaws.com/churn-mlops:${{ github.sha }}|" helm/values.yaml
          git config user.name "GitHub Actions"
          git config user.email "actions@github.com"
          git add helm/values.yaml
          git commit -m "Update image to ${{ github.sha }}"
          git push
```

## Cost Estimation

| Service | Instance Type | Hourly | Monthly (730h) |
|---------|---------------|--------|----------------|
| EKS | t3.large × 3 | $0.26 | $190 |
| RDS | db.t3.micro | $0.02 | $15 |
| S3 | Data storage | Variable | $1-10 |
| NAT Gateway | - | $0.045 | $33 |
| **Total** | | | **~$240/month** |

**Cost optimization tips:**
- Use Spot instances (70% savings): `terraform apply -var="use_spot=true"`
- Scale down at night: Karpenter consolidation
- Archive old training runs in S3 Glacier

## Troubleshooting

### Issue: EKS cluster creation fails
```bash
# Check IAM permissions
aws iam get-user

# Check service quotas
aws service-quotas list-service-quotas --service-code eks
```

### Issue: Pods stuck in Pending
```bash
# Check node capacity
kubectl describe nodes

# Check events
kubectl get events -A --sort-by='.lastTimestamp'

# Scale up nodes
kubectl scale deployment argocd-server --replicas=2 -n argocd
```

### Issue: Airflow DAG not triggering
```bash
# Check Airflow logs
kubectl logs -f deployment/airflow-scheduler -n airflow

# Check DAG syntax
python -m py_compile airflow/dags/training_pipeline.py

# Restart Airflow
kubectl rollout restart deployment/airflow-webserver -n airflow
```

### Issue: Model inference returns errors
```bash
# Check inference service logs
kubectl logs -f deployment/fastapi-service -n inference

# Check model registry
kubectl port-forward svc/mlflow -n mlflow 5000:5000
# Verify model is registered in MLflow UI

# Redeploy with specific model version
kubectl set env deployment/fastapi-service \
  MODEL_VERSION=5 -n inference
```

## Next Steps

1. ✅ Deploy infrastructure (Phase 1-2)
2. ✅ Deploy platform services (Phase 3)
3. ✅ Setup data pipeline (Phase 4)
4. ✅ Train first model (Phase 5)
5. ✅ Deploy inference service (Phase 6)
6. ✅ Setup monitoring (Phase 7)
7. ✅ Configure CI/CD (Phase 8)
8. 🔄 **Scale to Production:**
   - Deploy to prod environment
   - Setup backup/disaster recovery
   - Configure multi-region failover
   - Implement canary deployments
   - Setup 24/7 on-call rotation

## Support & Documentation

- **Architecture:** [docs/architecture.md](docs/architecture.md)
- **Runbooks:** [docs/runbooks/](docs/runbooks/)
- **Main README:** [README.md](README.md)
- **GitHub Issues:** [Issues](https://github.com/ManimaranRepos/churn-mlops-platform/issues)

---

**Time estimate:** 4-6 hours for complete setup (can be done in one sitting)