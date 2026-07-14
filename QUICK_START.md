# Churn MLOps Platform - Quick Start Guide

## 5-Minute Local Demo (No AWS Required)

```bash
# Clone the repo
git clone https://github.com/ManimaranRepos/churn-mlops-platform.git
cd churn-mlops-platform

# Install dependencies
pip install -r requirements.txt

# Run demo training
python ml/training/train.py --local --sample-size 1000

# Run inference
python ml/inference/predict.py --input data/sample.csv
```

## Key Project Features

### 🏗️ **Architecture Diagram**
```
Kinesis Stream → S3 (Raw) → Glue ETL → S3 (Curated)
                                ↓
                         Great Expectations
                                ↓
                    Feature Store (S3)
                                ↓
            ┌─────────────────────────────────┐
            │                                 │
        SageMaker                         MLflow
        Training                          Registry
            │                                 │
            └────────────┬────────────────────┘
                         ↓
                   SageMaker Endpoint
                         ↓
                    FastAPI Service
                         ↓
                  API Consumers
```

### 📊 **What's Included**

| Component | Purpose | Status |
|-----------|---------|--------|
| Data Pipeline | Kinesis → S3 → Glue ETL | ✅ Complete |
| Feature Engineering | Feature store + validation | ✅ Complete |
| Model Training | XGBoost + PyTorch with HPO | ✅ Complete |
| Experiment Tracking | MLflow registry | ✅ Complete |
| Inference | FastAPI + SageMaker | ✅ Complete |
| Orchestration | Airflow DAGs (4 pipelines) | ✅ Complete |
| DevOps | ArgoCD + Helm + Terraform | ✅ Complete |
| Monitoring | CloudWatch + Prometheus | ✅ Complete |

### 🚀 **Production Deployment**

**Estimated time:** 3-4 hours on a fresh AWS account

Prerequisites:
- AWS CLI (configured)
- Terraform ≥ 1.6
- kubectl
- Helm 3
- Docker
- Python 3.11+

**Deploy:**
```bash
# 1. Bootstrap Terraform state
cd terraform/bootstrap
terraform init && terraform apply

# 2. Deploy infrastructure
cd ../environments/dev
terraform init && terraform apply

# 3. Deploy platform via GitOps
kubectl apply -f ../../argocd/bootstrap/app-of-apps.yaml

# 4. Monitor deployment
kubectl get pods -n argocd
kubectl port-forward -n argocd svc/argocd-server 8080:443
# Access at https://localhost:8080
```

## Use Cases

**This platform can power:**

- ✅ Customer retention campaigns
- ✅ Churn prediction for telecom/SaaS
- ✅ Risk scoring
- ✅ Demand forecasting
- ✅ Any binary classification at scale

## Architecture Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| **Cloud** | AWS | Mature, widely used in enterprises |
| **Compute** | EKS + Karpenter | Auto-scaling Kubernetes |
| **Data Ingestion** | Kinesis | Real-time streaming |
| **ETL** | Glue + Iceberg | Serverless, ACID transactions |
| **ML Ops** | SageMaker + MLflow | End-to-end ML lifecycle |
| **Inference** | FastAPI | Fast Python API |
| **Orchestration** | Airflow | Complex DAG workflows |
| **GitOps** | ArgoCD | Infrastructure as code |
| **IaC** | Terraform | Reproducible infrastructure |
| **Monitoring** | CloudWatch + Prometheus | Comprehensive observability |

## Key Concepts Demonstrated

### 1. **Data Quality**
- Great Expectations for validation
- Automated drift detection
- Schema enforcement

### 2. **Model Training**
- Hyperparameter optimization
- Cross-validation
- Model versioning in MLflow

### 3. **Canary Deployments**
- Gradual rollout (10% → 50% → 100%)
- Automated rollback on poor performance
- A/B testing ready

### 4. **Observability**
- Model performance monitoring
- Data drift detection
- Inference latency tracking

### 5. **Security**
- IAM role-based access
- KMS encryption
- Pod security policies
- Network policies

## File Structure Explained

```
churn-mlops-platform/
├── airflow/
│   ├── dags/
│   │   ├── feature_pipeline.py         # Daily feature generation
│   │   ├── training_pipeline.py        # Weekly model training
│   │   ├── monitoring_pipeline.py      # Drift detection
│   │   └── data_quality_pipeline.py    # Validation
│   └── Dockerfile                      # Airflow container
│
├── ml/
│   ├── training/
│   │   ├── train.py                    # XGBoost + PyTorch training
│   │   └── hyperparameter_tuning.py    # SageMaker HPO
│   ├── inference/
│   │   └── predict.py                  # Batch predictions
│   ├── monitoring/
│   │   └── drift_detection.py          # Model performance monitoring
│   └── deployment/
│       ├── canary_deploy.py            # Gradual rollout
│       └── rollback.py                 # Emergency rollback
│
├── inference/
│   ├── main.py                         # FastAPI service
│   ├── Dockerfile
│   └── helm/                           # Kubernetes deployment
│
├── terraform/
│   ├── bootstrap/                      # S3 backend setup
│   ├── modules/
│   │   ├── vpc/                        # Network
│   │   ├── eks/                        # Kubernetes cluster
│   │   ├── s3/                         # Data lake
│   │   ├── iam/                        # Access control
│   │   ├── kms/                        # Encryption
│   │   ├── monitoring/                 # CloudWatch + Prometheus
│   │   └── security/                   # GuardDuty, OPA/Gatekeeper
│   └── environments/
│       ├── dev/
│       ├── staging/
│       └── prod/
│
├── argocd/
│   ├── bootstrap/
│   │   └── app-of-apps.yaml           # GitOps root
│   └── overlays/
│       ├── dev/
│       ├── staging/
│       └── prod/
│
├── monitoring/
│   ├── prometheus/
│   │   └── rules.yaml                 # Alert rules
│   └── grafana/
│       └── dashboards/                # Pre-built dashboards
│
├── docs/
│   ├── architecture.md                # Full design document
│   ├── deployment_guide.md            # Step-by-step setup
│   └── runbooks/
│       ├── inference_down.md          # Troubleshooting
│       ├── pipeline_failure.md
│       ├── model_drift.md
│       ├── emergency_rollback.md
│       └── security_incident.md
│
└── scripts/
    ├── security_scan.sh               # CVE scanning
    └── github_setup.sh                # GitHub Actions secrets
```

## Operational Runbooks

**When something breaks, these guides help:**

1. **Inference Service Down** → [docs/runbooks/inference_down.md](docs/runbooks/inference_down.md)
2. **Pipeline Failure** → [docs/runbooks/pipeline_failure.md](docs/runbooks/pipeline_failure.md)
3. **Model Drift Detected** → [docs/runbooks/model_drift.md](docs/runbooks/model_drift.md)
4. **Emergency Rollback** → [docs/runbooks/emergency_rollback.md](docs/runbooks/emergency_rollback.md)
5. **Security Incident** → [docs/runbooks/security_incident.md](docs/runbooks/security_incident.md)

## Learning Path

**For UAE/Dubai ML Engineering roles, this project demonstrates:**

1. ✅ Full ML lifecycle (data → training → serving)
2. ✅ Production patterns (canary deploy, monitoring, rollback)
3. ✅ Cloud-native architecture (Kubernetes, Terraform)
4. ✅ Data engineering (streaming, ETL, validation)
5. ✅ DevOps & GitOps (CI/CD, infrastructure-as-code)
6. ✅ Security & compliance (IAM, encryption, monitoring)

## Next Level Enhancements

- [ ] Real-time inference (reduce 500ms batch to <100ms)
- [ ] Multi-armed bandit for A/B testing
- [ ] Federated learning for privacy
- [ ] LLM-powered debugging (AI explains model decisions)
- [ ] Zero-downtime deployments with blue-green

## Questions?

- **For architecture:** See [docs/architecture.md](docs/architecture.md)
- **For deployment:** See [DEPLOYMENT.md](DEPLOYMENT.md)
- **For troubleshooting:** See [docs/runbooks/](docs/runbooks/)

---

**Ready to deploy to production?** Start with [DEPLOYMENT.md](DEPLOYMENT.md)