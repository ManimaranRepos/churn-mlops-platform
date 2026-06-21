# Churn MLOps Platform

Production-grade MLOps platform on AWS for predicting customer churn — built across 11 phases covering the full ML lifecycle from data ingestion to automated retraining.

## Architecture

```
Application Events
       │
       ▼
 Kinesis Data Stream
       │
       ▼
Kinesis Firehose ──► S3 (raw)
                          │
                     Glue ETL
                          │
                     S3 (curated, Iceberg)
                          │
               Great Expectations (validation)
                          │
                   Feature Store (S3)
                          │
              ┌───────────┴───────────┐
              │                       │
        SageMaker                  MLflow
        Training                  (registry)
              │                       │
              └───────────┬───────────┘
                          │
                  SageMaker Endpoint
                          │
              FastAPI Inference Service
                    (EKS + Helm)
                          │
                   API Gateway
                          │
                    Consumers
```

## Stack

| Layer | Technology |
|---|---|
| Cloud | AWS (us-east-1) |
| Compute | EKS (Karpenter autoscaling) |
| Data ingestion | Kinesis → Firehose → S3 Iceberg |
| ETL | AWS Glue |
| Data validation | Great Expectations |
| Model training | SageMaker (XGBoost + PyTorch) |
| Experiment tracking | MLflow |
| Inference | FastAPI + SageMaker endpoint + ElastiCache |
| Orchestration | Apache Airflow |
| GitOps | ArgoCD + Helm |
| Infrastructure | Terraform |
| Monitoring | CloudWatch + Prometheus + Grafana |
| CI/CD | GitHub Actions |
| Security | GuardDuty, Security Hub, KMS, OPA/Gatekeeper |

## Repository Structure

```
churn-mlops-platform/
├── airflow/              # DAGs: feature pipeline, training, monitoring, data quality
├── argocd/               # App-of-apps bootstrap + per-environment overlays
├── data_pipeline/        # Kinesis producers, Lambda transformers, Glue ETL scripts
├── docs/
│   ├── architecture.md   # Full architecture document with diagrams
│   ├── deployment_guide.md
│   └── runbooks/         # 5 operational runbooks (inference, pipeline, drift, rollback, security)
├── helm/                 # Base platform Helm values (ALB, cert-manager, Karpenter…)
├── inference/            # FastAPI service + Dockerfile + Helm chart
├── k8s/                  # Gatekeeper policies, RBAC, namespace quotas
├── ml/
│   ├── training/         # XGBoost and PyTorch training scripts
│   ├── evaluation/       # Model evaluation and threshold checks
│   ├── deployment/       # Canary deploy, promote, rollback scripts
│   ├── monitoring/       # Drift detection, baseline capture, ground truth collection
│   └── mlflow/           # MLflow tracking server (Dockerized)
├── monitoring/           # Prometheus rules, Grafana dashboard, AlertManager config
├── scripts/              # Security scan, GitHub environment setup
├── terraform/            # Modules: VPC, EKS, S3, IAM, KMS, API Gateway, monitoring, security
└── CLAUDE.md             # AI-assistant context for this repo
```

## Phases Built

| Phase | Scope |
|---|---|
| 1 | Data ingestion — Kinesis → Firehose → S3 |
| 2 | Glue ETL → Iceberg curated layer |
| 3 | Great Expectations data validation |
| 4 | Feature engineering pipeline |
| 5 | SageMaker model training (XGBoost + PyTorch HPO) |
| 6 | MLflow experiment tracking + model registry |
| 7 | FastAPI inference service + SageMaker endpoint |
| 8 | Airflow orchestration (4 DAGs) |
| 9 | ArgoCD GitOps + Helm multi-env deployment |
| 10 | CI/CD (GitHub Actions), monitoring (CloudWatch + Grafana), security hardening |
| 11 | Documentation, architecture diagrams, 5 operational runbooks |

## Operational Runbooks

- [Inference Service Down](docs/runbooks/inference_down.md)
- [Feature / Training Pipeline Failure](docs/runbooks/pipeline_failure.md)
- [Model Drift Detected](docs/runbooks/model_drift.md)
- [Emergency Model Rollback](docs/runbooks/emergency_rollback.md)
- [Security Incident Response](docs/runbooks/security_incident.md)

## Getting Started

See [docs/deployment_guide.md](docs/deployment_guide.md) for the full zero-to-production walkthrough (~3–4 hours for a greenfield AWS account).

**Prerequisites:** AWS CLI, Terraform ≥ 1.6, kubectl, Helm 3, Docker, Python 3.11+

```bash
# Bootstrap Terraform state backend
cd terraform/bootstrap && terraform init && terraform apply

# Deploy core infrastructure
cd terraform/environments/dev && terraform init && terraform apply

# Deploy platform services via ArgoCD
kubectl apply -f argocd/bootstrap/app-of-apps.yaml
```
