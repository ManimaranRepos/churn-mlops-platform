# Churn Platform — Claude Code Context

## What this project is
Production-grade MLOps platform on AWS for predicting customer churn.
Greenfield AWS account, region `us-east-1`. Built across 11 phases.

## Architecture in one paragraph
Events stream from the application → Kinesis → Firehose → S3 (raw).
A daily Airflow DAG runs Glue ETL jobs to produce Iceberg curated tables,
engineers features, validates data with Great Expectations, then triggers
a training pipeline that trains XGBoost and PyTorch models in parallel on
SageMaker Spot. The winner is promoted through MLflow Model Registry
(Staging → Production), deployed as a canary (10% ALB traffic) via a
FastAPI server on EKS, and fully promoted after 30 minutes of healthy
metrics. Redis caches predictions. SageMaker Model Monitor runs every 6h
detecting drift; exceeding thresholds auto-triggers retraining via the
Airflow REST API. Prometheus + Grafana cover EKS metrics; CloudWatch
covers AWS-native metrics. OPA Gatekeeper enforces pod security policies.
GuardDuty + Security Hub + Config provide compliance and threat detection.

## Repository layout
```
airflow/          Airflow DAGs, Helm values, Dockerfile
  dags/           feature_pipeline, training_pipeline, data_quality, model_monitoring
  helm/           Airflow Helm values (KubernetesExecutor, gitSync, S3 logging)
  terraform/      IRSA role for Airflow pods

argocd/           ArgoCD Applications for all platform services
  apps/           airflow.yaml, inference.yaml, monitoring.yaml, gatekeeper.yaml, mlflow.yaml
  overlays/       Per-environment Helm value overrides (dev/staging/prod)

data_pipeline/    Glue ETL jobs (raw→curated, feature engineering)

docs/             Architecture, deployment guide, operations guide
  runbooks/       Operational runbooks for common incident types

inference/        FastAPI inference server
  src/            main.py (FastAPI), predictor.py (XGBoost+PyTorch), cache.py (Redis)
  helm/           Kubernetes Deployment, HPA, PDB, Ingress values
  terraform/      IRSA role for inference pods

k8s/              Kubernetes manifests applied outside Helm
  gatekeeper/     OPA Gatekeeper ConstraintTemplates + Constraints
  namespaces/     Namespace definitions, ResourceQuotas, Karpenter NodePools
  rbac/           ClusterRoles and RoleBindings

ml/               ML code
  training/       XGBoost trainer, PyTorch trainer, SageMaker job launcher, HPO config
  evaluation/     evaluate_model.py — held-out test set evaluation + quality gates
  validation/     validate_data.py — 7 data quality checks + Great Expectations
  deployment/     canary_deploy.py, promote_model.py, rollback.py
  monitoring/     baseline_capture.py, drift_detector.py, ground_truth_collector.py

monitoring/       kube-prometheus-stack Helm values, PrometheusRules, ServiceMonitors
  alerts/         churn_rules.yaml, service_monitors.yaml
  dashboards/     Grafana dashboard JSON + ConfigMap

scripts/          Operational scripts
  security_scan.sh  Trivy + Checkov CI security gate

terraform/
  modules/
    elasticache/  Redis cluster for prediction cache
    api_gateway/  HTTP API v2 + VPC Link + Lambda authoriser
    model_monitor/ SageMaker Model Monitor schedules + drift detector Lambda
    monitoring/   SNS topics, Slack forwarder Lambda, CloudWatch dashboards
    security/     GuardDuty, Security Hub, Config, Access Analyzer, CloudTrail
```

## Non-obvious design decisions
- **KubernetesExecutor in Airflow**: each task runs as an ephemeral pod (zero idle cost).
  Worker image = same as scheduler image. Changing the image requires a rolling restart
  of scheduler + webserver, not just a DAG update.
- **gitSync for DAGs**: DAGs are synced from Git into the scheduler pod every 60s.
  A DAG change is live without any pod restart. Don't put large files in `airflow/dags/`.
- **SageMaker Spot training**: `MaxWaitTimeInSeconds=86400` (24h). If Spot capacity is
  unavailable, the job waits up to 24h before failing. The Airflow task will appear stuck.
  Check SageMaker console for "WaitingForCapacity" status.
- **Model threshold is NOT 0.5**: XGBoost and PyTorch both search thresholds 0.1–0.9
  to maximise F1 on the validation set. The winning threshold is stored in
  `inference_metadata.json` inside the MLflow artifact. The inference server reads it
  at startup. Never hardcode 0.5 for churn (imbalanced class).
- **Ground truth lag**: Model Quality Monitor needs CRM churn outcomes which arrive
  30–90 days after predictions. The weekly `churn_model_monitoring` DAG joins predictions
  (from Data Capture) to outcomes. Don't expect model quality metrics on day 1.
- **Aurora Serverless v2**: scales to 0 ACUs after 5 min idle (dev only). The first
  Airflow scheduler heartbeat after a quiet period may time out waiting for Aurora to
  wake. Set `min_capacity=0.5` in prod to avoid this.
- **ALB target groups**: canary_deploy.py uses two weighted target groups. The
  `stable` TG always exists. The `canary` TG is created by the Helm chart and weighted
  to 0% until a deployment starts. Never delete the `canary` TG or the ALB routing breaks.

## Security rules (must never be broken)
- All secrets via AWS Secrets Manager or SSM — never hardcoded, never in ConfigMaps
- Every container: non-root user, resource requests+limits, pinned image tag (enforced by Gatekeeper)
- All AWS resources tagged: Environment, Team, CostCenter, Project, ManagedBy=terraform
- IRSA for all pod AWS access — no access keys in pods
- All S3 buckets: SSE-KMS, block public access, TLS-only bucket policy

## CI/CD pipeline
GitHub Actions → ECR build → `security_scan.sh` (Trivy + Checkov) → ArgoCD sync.
ArgoCD Applications are in `argocd/apps/`. Image tags are injected via
`argocd app set <app> --helm-set image.tag=<sha>`.

## Key environment variables (set in GitHub Secrets)
- `AWS_ACCOUNT_ID`, `AWS_REGION` (us-east-1)
- `ECR_REGISTRY` — `<account>.dkr.ecr.us-east-1.amazonaws.com`
- `SAGEMAKER_ROLE_ARN`, `SAGEMAKER_VPC_SUBNETS`, `SAGEMAKER_VPC_SECURITY_GROUPS`
- `MLFLOW_TRACKING_URI` — internal ALB DNS for MLflow
- `ARTIFACTS_BUCKET`, `RAW_BUCKET`, `PROCESSED_BUCKET`
- `ENVIRONMENT` — dev | staging | prod

## Cost guardrail
AWS Budget alert at $400 (80%) and $500 (100%) — subscriber: smanimarancse@gmail.com.
Spot instances used for: SageMaker training, Airflow worker pods (dev), EKS dev nodes.
On-demand required for: inference pods (prod), Aurora, ElastiCache.
