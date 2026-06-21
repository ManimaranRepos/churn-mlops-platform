# Platform Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CHURN PREDICTION PLATFORM                           │
│                              AWS us-east-1                                  │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════
  DATA INGESTION (Phase 2)
═══════════════════════════════════════════════════════════════════

  Application          Kinesis Data         Kinesis Data
  (CRM / web)  ──────► Stream               Firehose
                        (churn-events)  ───► (buffer 5min/128MB)
                        3 shards             │
                                             ▼
                                      S3 Raw Bucket
                                      /events/YYYY/MM/DD/HH/
                                      (Parquet, partitioned)

═══════════════════════════════════════════════════════════════════
  DATA PIPELINE — AIRFLOW DAG: churn_feature_pipeline (02:00 UTC)
═══════════════════════════════════════════════════════════════════

  S3 Raw Bucket
       │
       ▼ S3KeySensor (wait for partition)
  ┌─────────────────────────────────────────────────────────────┐
  │  AWS Glue ETL                                               │
  │  Job: raw_to_curated   ──► Iceberg table (curated DB)       │
  │  Job: feature_eng      ──► Feature Parquet (processed S3)   │
  │  Crawler: updates Glue Data Catalog                         │
  └─────────────────────────────────────────────────────────────┘
       │
       ▼ validate_data.py (7 checks + Great Expectations)
  ┌─────────────────┐
  │ ShortCircuit    │ ◄── Stops pipeline if validation fails
  │ Gate            │
  └─────────────────┘
       │
       ▼ export_splits (80/10/10 stratified)
  s3://processed/features/{train,validation,test}/{execution_date}/
       │
       ▼ TriggerDagRunOperator (wait_for_completion=False)
  churn_training_pipeline ──────────────────────────────────────►

═══════════════════════════════════════════════════════════════════
  ML TRAINING (Phase 5) — AIRFLOW DAG: churn_training_pipeline
═══════════════════════════════════════════════════════════════════

                    ┌──────────────────────────────┐
                    │     Parallel SageMaker Jobs   │
                    │  (Spot, ml.m5.xlarge, VPC)   │
                    │                              │
              ┌─────┴────┐              ┌──────────┴───┐
              │ XGBoost  │              │   PyTorch    │
              │ trainer  │              │   trainer    │
              │ hist trees│             │ MLP+FocalLoss│
              │ scale_pos │             │ TorchScript  │
              │ _weight   │             │ export       │
              └─────┬────┘              └──────────┬───┘
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  evaluate_model.py  │
                    │  (held-out test set)│
                    │  Quality gates:     │
                    │  AUC ≥ 0.82         │
                    │  Precision ≥ 0.75   │
                    │  Recall ≥ 0.70      │
                    │  P99 < 200ms        │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ BranchPythonOp      │
                    │ pick_winner         │
                    │ (compare AUC+F1)    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  MLflow Model       │
                    │  Registry           │
                    │  Staging → Prod     │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  canary_deploy.py   │
                    │  10% canary traffic │
                    │  Monitor 30 min     │
                    │  Auto-rollback on   │
                    │  error>5%/P99>500ms │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  promote_model.py   │
                    │  100% traffic       │
                    │  MLflow: Production │
                    └─────────────────────┘

═══════════════════════════════════════════════════════════════════
  MODEL SERVING (Phase 7) — EKS, us-east-1 (3 AZs)
═══════════════════════════════════════════════════════════════════

  External Caller (CRM)
       │
       ▼ HTTPS + X-Api-Key header
  ┌─────────────────────────────┐
  │  API Gateway HTTP API v2    │
  │  Lambda Authoriser          │
  │  (validates API key via SM) │
  └──────────────┬──────────────┘
                 │ VPC Link (private)
  ┌──────────────▼──────────────┐
  │  Internal ALB               │
  │  Weighted Target Groups:    │
  │  stable (100%) / canary(0%) │
  └──────────────┬──────────────┘
                 │
  ┌──────────────▼──────────────────────────────────────────┐
  │  EKS — namespace: inference                              │
  │                                                          │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
  │  │  FastAPI pod │  │  FastAPI pod │  │  FastAPI pod │  │
  │  │  (AZ-a)      │  │  (AZ-b)      │  │  (AZ-c)      │  │
  │  │  2 Gunicorn  │  │  2 Gunicorn  │  │  2 Gunicorn  │  │
  │  │  workers     │  │  workers     │  │  workers     │  │
  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
  │         │                 │                  │          │
  │         └─────────────────┴──────────────────┘          │
  │                           │                             │
  │              ┌────────────▼────────────┐                │
  │              │  ElastiCache Redis       │                │
  │              │  Primary (AZ-a)          │                │
  │              │  Replica (AZ-b)          │                │
  │              │  TTL=300s, allkeys-lru   │                │
  │              └─────────────────────────┘                │
  │                                                          │
  │  Model loaded from MLflow at pod startup                 │
  │  XGBoost (model.json) or PyTorch (TorchScript .pt)       │
  └──────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════
  MODEL MONITORING (Phase 9) — SageMaker Model Monitor
═══════════════════════════════════════════════════════════════════

  FastAPI pods ──► Data Capture ──► S3
                                    │
                    ┌───────────────▼──────────────────┐
                    │  SageMaker Model Monitor          │
                    │  DataQuality  (every 6h)          │
                    │  ModelQuality (every 6h + 30min)  │
                    └───────────────┬──────────────────┘
                                    │ EventBridge on completion
                    ┌───────────────▼──────────────────┐
                    │  Drift Detector Lambda             │
                    │  drift_score = Σ(violations×weight)│
                    │  if score ≥ 0.3 → retrain         │
                    └───────────────┬──────────────────┘
                                    │ POST /api/v1/dagRuns
                    ┌───────────────▼──────────────────┐
                    │  Airflow: churn_training_pipeline │
                    └───────────────────────────────────┘

  CRM (churn outcomes)
       │ weekly Parquet
       ▼
  ground_truth_collector.py
       │ joins predictions + outcomes
       ▼
  merged_labels.csv ──► Model Quality Monitor (label input)

═══════════════════════════════════════════════════════════════════
  OBSERVABILITY (Phase 8)
═══════════════════════════════════════════════════════════════════

  EKS pods                   CloudWatch
  (Prometheus /metrics)      (AWS-native metrics)
        │                           │
        ▼                           ▼
  ┌─────────────┐        ┌──────────────────┐
  │  Prometheus │        │  CloudWatch       │
  │  (EKS)      │        │  Dashboards       │
  │  15d retent │        │  (API GW, Redis,  │
  └──────┬──────┘        │   Kinesis, SM)    │
         │               └──────────────────┘
         ▼
  ┌─────────────┐
  │  Grafana    │ ◄── dashboards from ConfigMaps (Git)
  │  (EKS)      │ ◄── CloudWatch datasource (IRSA)
  └──────┬──────┘
         │ alert
         ▼
  ┌─────────────────────────────────────────┐
  │  AlertManager                           │
  │  → alertmanager-sns-forwarder sidecar   │
  │  → SNS (critical / warning / info)      │
  │  → Lambda (slack_lambda)                │
  │  → Slack (#alerts-critical, #ml-platform)│
  └─────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════
  SECURITY (Phase 10)
═══════════════════════════════════════════════════════════════════

  ┌────────────────────────────────────────────────────────────┐
  │  GuardDuty (EKS audit + S3 + malware protection)           │
  │  Security Hub (CIS v1.4 + AWS FSBP)                        │
  │  Config (9 managed rules, continuous recording)             │
  │  IAM Access Analyzer (cross-account resource exposure)      │
  │  CloudTrail (multi-region, S3 data events, 1yr retention)   │
  │  VPC Flow Logs (all traffic, 30d retention)                 │
  │  7× CloudWatch metric filters on CloudTrail (CIS alarms)   │
  └──────────────────────────────────┬─────────────────────────┘
                                     │ CRITICAL/HIGH findings
                                     ▼
                              EventBridge → Lambda
                              → SNS → Slack #alerts-critical

  ┌────────────────────────────────────────────────────────────┐
  │  OPA Gatekeeper (admission control)                         │
  │  • RequireResourceLimits — CPU+memory requests+limits       │
  │  • RequireNonRoot — UID != 0, runAsNonRoot: true            │
  │  • DisallowLatestTag — pinned tag or SHA digest             │
  │  Applied to: inference, airflow, mlflow, monitoring ns      │
  └────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════
  GITOPS / CI-CD
═══════════════════════════════════════════════════════════════════

  GitHub PR
       │ push
       ▼
  GitHub Actions
  ├── security_scan.sh (Trivy + Checkov) ── FAIL → blocks merge
  ├── docker build + push → ECR (tagged with git SHA)
  ├── python tests (pytest)
  └── argocd app set <app> --helm-set image.tag=<sha>
                │
                ▼
          ArgoCD (EKS)
          ├── airflow          (kube-prometheus-stack chart + git values)
          ├── churn-inference  (inference/helm)
          ├── monitoring       (kube-prometheus-stack chart + git values)
          ├── gatekeeper       (gatekeeper chart + k8s/gatekeeper)
          └── mlflow           (custom chart)
```

## Data Flow: Prediction Request

```
CRM system
  │  POST /predict  {"customer_id": "C123", "features": {...}}
  │  Header: X-Api-Key: <key>
  ▼
API Gateway HTTP API (HTTPS, public)
  │  Lambda Authoriser validates key against Secrets Manager
  │  VPC Link routes to internal ALB
  ▼
Internal ALB  (100% → stable TG  OR  90/10 during canary)
  ▼
FastAPI pod (EKS, namespace: inference)
  │
  ├─► Redis GET hash(customer_id + features)  ──► HIT → return cached result (< 2ms)
  │                                               │
  │                                               MISS ▼
  │
  ├─► preprocessor.pkl  (sklearn Pipeline: impute → encode → scale)
  │
  ├─► model (XGBoost or PyTorch TorchScript, loaded at startup from MLflow)
  │
  ├─► threshold lookup (from inference_metadata.json, e.g. 0.42)
  │
  ├─► Redis SET hash → result (TTL=300s)
  │
  └─► Response: {"churn_probability": 0.73, "churn_prediction": true,
                  "model_version": "12", "threshold": 0.42, "cached": false,
                  "latency_ms": 18.4}
```

## Component Ownership

| Component              | Code location                         | Deployed by         | Runtime           |
|------------------------|---------------------------------------|---------------------|-------------------|
| Data ingestion         | `data_pipeline/` (Glue)              | Terraform           | AWS Glue          |
| Feature pipeline DAG   | `airflow/dags/feature_pipeline.py`   | ArgoCD (gitSync)    | Airflow on EKS    |
| Training pipeline DAG  | `airflow/dags/training_pipeline.py`  | ArgoCD (gitSync)    | Airflow on EKS    |
| XGBoost trainer        | `ml/training/xgboost/train.py`       | GitHub Actions      | SageMaker Spot    |
| PyTorch trainer        | `ml/training/pytorch/train.py`       | GitHub Actions      | SageMaker Spot    |
| Model evaluation       | `ml/evaluation/evaluate_model.py`    | GitHub Actions      | SageMaker         |
| Canary deployment      | `ml/deployment/canary_deploy.py`     | Airflow DAG task    | Airflow pod       |
| Inference server       | `inference/src/main.py`              | ArgoCD              | EKS (inference ns)|
| Prediction cache       | `inference/src/cache.py`             | —                   | ElastiCache Redis |
| Model monitoring       | `ml/monitoring/drift_detector.py`    | EventBridge Lambda  | Lambda + SM       |
| Metrics + alerting     | `monitoring/`                        | ArgoCD              | EKS (monitoring)  |
| Security controls      | `terraform/modules/security/`        | Terraform           | AWS-managed       |
| Pod security policies  | `k8s/gatekeeper/`                    | ArgoCD              | Gatekeeper on EKS |
