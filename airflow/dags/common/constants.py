"""
Shared constants for all DAGs.
Centralised here so a bucket rename is one change, not a grep-and-replace.
"""

import os

ENVIRONMENT     = os.environ.get("ENVIRONMENT", "dev")
PROJECT         = os.environ.get("PROJECT", "churn-platform")
AWS_REGION      = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
AWS_ACCOUNT_ID  = os.environ.get("AWS_ACCOUNT_ID", "")

# ── S3 ─────────────────────────────────────────────────────────────────────────
RAW_BUCKET        = f"{PROJECT}-{ENVIRONMENT}-raw"
PROCESSED_BUCKET  = f"{PROJECT}-{ENVIRONMENT}-processed"
ARTIFACTS_BUCKET  = f"{PROJECT}-{ENVIRONMENT}-artifacts"

# ── Glue ───────────────────────────────────────────────────────────────────────
GLUE_RAW_DATABASE      = f"{PROJECT.replace('-', '_')}_{ENVIRONMENT}_raw"
GLUE_CURATED_DATABASE  = f"{PROJECT.replace('-', '_')}_{ENVIRONMENT}_curated"
GLUE_JOB_RAW_TO_CURATED    = f"{PROJECT}-{ENVIRONMENT}-raw-to-curated"
GLUE_JOB_FEATURE_ENGINEERING = f"{PROJECT}-{ENVIRONMENT}-feature-engineering"
GLUE_CRAWLER_RAW     = f"{PROJECT}-{ENVIRONMENT}-raw-crawler"
GLUE_CRAWLER_CURATED = f"{PROJECT}-{ENVIRONMENT}-curated-crawler"

# ── Athena ─────────────────────────────────────────────────────────────────────
ATHENA_WORKGROUP = f"{PROJECT}-{ENVIRONMENT}"

# ── Kinesis ────────────────────────────────────────────────────────────────────
KINESIS_STREAM_NAME = f"{PROJECT}-{ENVIRONMENT}-events"

# ── SageMaker ─────────────────────────────────────────────────────────────────
SAGEMAKER_EXECUTION_ROLE_ARN = os.environ.get("SAGEMAKER_EXECUTION_ROLE_ARN", "")
SAGEMAKER_VPC_SUBNETS        = os.environ.get("SAGEMAKER_VPC_SUBNETS", "")
SAGEMAKER_VPC_SG             = os.environ.get("SAGEMAKER_VPC_SECURITY_GROUPS", "")

# ── MLflow ─────────────────────────────────────────────────────────────────────
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow.mlops.svc.cluster.local:5000")
MLFLOW_MODEL_NAME_XGBOOST = "churn-prediction-xgboost"
MLFLOW_MODEL_NAME_PYTORCH  = "churn-prediction-pytorch"

# ── Quality gate thresholds ────────────────────────────────────────────────────
QUALITY_GATE_AUC        = 0.82
QUALITY_GATE_PRECISION  = 0.75
QUALITY_GATE_RECALL     = 0.70
QUALITY_GATE_LATENCY_MS = 200.0

# ── Data pipeline S3 paths ─────────────────────────────────────────────────────
FEATURES_S3_PREFIX = f"s3://{PROCESSED_BUCKET}/features"
TRAIN_S3_PATH      = f"{FEATURES_S3_PREFIX}/train/"
VAL_S3_PATH        = f"{FEATURES_S3_PREFIX}/validation/"
TEST_S3_PATH       = f"{FEATURES_S3_PREFIX}/test/"
