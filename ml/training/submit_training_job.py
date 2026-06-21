"""
Submit a SageMaker Training Job for churn prediction.

WHY a standalone launcher instead of boto3 inline in GitHub Actions?
  1. The job logic (spot config, HPO, hyperparams) belongs in code, not YAML
  2. We can run this locally for debugging without touching CI
  3. The same script runs both XGBoost and PyTorch jobs via --model-type flag
  4. SageMaker Experiments + MLflow run tracking are wired up here once

Usage (called from .github/workflows/ml-pipeline.yml):
  python submit_training_job.py \
      --model-type xgboost \
      --input-s3 s3://bucket/processed/features/ \
      --role-arn arn:aws:iam::123:role/sagemaker-training \
      --experiment-name churn-xgboost-dev \
      --wait

Exit codes:
  0 — job completed successfully
  1 — job failed or quality gates not met
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Container image URIs — built and pushed in .github/workflows/application.yml
ECR_IMAGES = {
    "xgboost": "{account_id}.dkr.ecr.{region}.amazonaws.com/churn-platform/training-xgboost:{tag}",
    "pytorch":  "{account_id}.dkr.ecr.{region}.amazonaws.com/churn-platform/training-pytorch:{tag}",
}

# Default hyperparameters per model type
DEFAULT_HYPERPARAMS = {
    "xgboost": {
        "max_depth":              "6",
        "learning_rate":          "0.05",
        "n_estimators":           "500",
        "early_stopping_rounds":  "30",
        "subsample":              "0.8",
        "colsample_bytree":       "0.8",
        "scale_pos_weight":       "auto",
        "min_child_weight":       "5",
        "gamma":                  "0.1",
        "eval_metric":            "auc",
        "tree_method":            "hist",
    },
    "pytorch": {
        "hidden_dims":   "256,128,64",
        "dropout_rate":  "0.3",
        "learning_rate": "0.001",
        "weight_decay":  "0.0001",
        "batch_size":    "512",
        "max_epochs":    "100",
        "patience":      "10",
        "focal_gamma":   "2.0",
    },
}

# SageMaker instance types
# WHY ml.m5.xlarge for XGBoost?
#   XGBoost is CPU-bound; GPU offers no speedup for tree methods.
#   ml.m5.xlarge (4 vCPU, 16GB) finishes in ~3 min — cheaper than GPU.
# WHY ml.p3.2xlarge for PyTorch?
#   MLP training is parallelised well on GPU.
#   But for our small dataset size, ml.m5.2xlarge (CPU) is fine and 10x cheaper.
INSTANCE_TYPES = {
    "xgboost": "ml.m5.xlarge",
    "pytorch":  "ml.m5.2xlarge",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Submit SageMaker training job")
    parser.add_argument("--model-type",  required=True, choices=["xgboost", "pytorch"])
    parser.add_argument("--input-s3",    required=True, help="S3 path to feature parquet files")
    parser.add_argument("--role-arn",    required=True, help="SageMaker execution role ARN")
    parser.add_argument("--account-id",  default=os.environ.get("AWS_ACCOUNT_ID"))
    parser.add_argument("--image-tag",   default=os.environ.get("GIT_SHA", "latest"))
    parser.add_argument("--experiment",  default=None,  help="MLflow experiment name")
    parser.add_argument("--output-s3",   default=None,  help="S3 path for model artifacts (defaults to artifacts bucket)")
    parser.add_argument("--artifacts-bucket", required=True, help="S3 artifacts bucket name")
    parser.add_argument("--use-spot",    action="store_true", default=True, help="Use Spot instances (default: True)")
    parser.add_argument("--no-spot",     dest="use_spot", action="store_false")
    parser.add_argument("--wait",        action="store_true", help="Wait for job completion")
    parser.add_argument("--hyperparams", default="{}", help="JSON string of additional hyperparameters")
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT", "dev"))
    return parser.parse_args()


def build_job_name(model_type: str) -> str:
    """Generate a unique, descriptive job name (max 63 chars, no underscores)."""
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    sha = os.environ.get("GIT_SHA", "local")[:8]
    return f"churn-{model_type}-{sha}-{ts}"


def submit_job(args, sm_client, job_name: str) -> dict:
    """
    Submit the SageMaker Training Job.

    Key decisions:
    - Spot instances with 24h max wait: reduces cost ~70%.
      SageMaker handles checkpointing on Spot interruption automatically
      (via the checkpoint_s3_uri parameter).
    - Input mode: File (copy to instance before training).
      Pipe mode is faster for large datasets but adds complexity.
    - Single input channel 'train' (train+val split is already on S3
      from the Airflow DAG — see Phase 6).
    """
    account_id = args.account_id or boto3.client("sts").get_caller_identity()["Account"]
    image_uri  = ECR_IMAGES[args.model_type].format(
        account_id=account_id,
        region=REGION,
        tag=args.image_tag,
    )

    output_path = (
        args.output_s3
        or f"s3://{args.artifacts_bucket}/sagemaker/training/{args.model_type}/{job_name}/"
    )
    checkpoint_path = f"s3://{args.artifacts_bucket}/sagemaker/checkpoints/{job_name}/"

    # Merge default + user-supplied hyperparams
    hyperparams = DEFAULT_HYPERPARAMS[args.model_type].copy()
    user_hps = json.loads(args.hyperparams)
    hyperparams.update({k: str(v) for k, v in user_hps.items()})

    # Pass MLflow tracking info as hyperparameters
    # (SageMaker env vars are limited; HP dict is the reliable way)
    hyperparams["mlflow_tracking_uri"] = os.environ.get(
        "MLFLOW_TRACKING_URI", "http://mlflow.mlops.svc.cluster.local:5000"
    )
    hyperparams["mlflow_experiment_name"] = (
        args.experiment or f"churn-{args.model_type}-{args.environment}"
    )

    job_config = {
        "TrainingJobName": job_name,
        "RoleArn":         args.role_arn,
        "AlgorithmSpecification": {
            "TrainingImage":     image_uri,
            "TrainingInputMode": "File",  # Data copied to instance before training
            "EnableSageMakerMetricsTimeSeries": True,  # Streams metrics to CloudWatch
            "MetricDefinitions": [
                # Regex patterns that SageMaker uses to parse stdout
                {"Name": "val:auc",       "Regex": r"val:auc: ([0-9\.]+)"},
                {"Name": "val:precision", "Regex": r"val:precision: ([0-9\.]+)"},
                {"Name": "val:recall",    "Regex": r"val:recall: ([0-9\.]+)"},
                {"Name": "val:f1",        "Regex": r"val:f1: ([0-9\.]+)"},
            ],
        },
        "HyperParameters": hyperparams,
        "InputDataConfig": [
            {
                "ChannelName": "train",
                "DataSource": {
                    "S3DataSource": {
                        "S3DataType":             "S3Prefix",
                        "S3Uri":                  f"{args.input_s3.rstrip('/')}/train/",
                        "S3DataDistributionType": "FullyReplicated",
                    }
                },
                "ContentType": "application/x-parquet",
                "CompressionType": "None",
                "InputMode": "File",
            },
            {
                "ChannelName": "validation",
                "DataSource": {
                    "S3DataSource": {
                        "S3DataType":             "S3Prefix",
                        "S3Uri":                  f"{args.input_s3.rstrip('/')}/validation/",
                        "S3DataDistributionType": "FullyReplicated",
                    }
                },
                "ContentType": "application/x-parquet",
                "CompressionType": "None",
                "InputMode": "File",
            },
        ],
        "OutputDataConfig": {
            "S3OutputPath": output_path,
            "KmsKeyId":     os.environ.get("KMS_KEY_ARN_S3", ""),  # Encrypt model artifacts
        },
        "ResourceConfig": {
            "InstanceType":   INSTANCE_TYPES[args.model_type],
            "InstanceCount":  1,
            "VolumeSizeInGB": 50,
        },
        "CheckpointConfig": {
            "S3Uri": checkpoint_path,  # SageMaker resumes from here on Spot interruption
        },
        "EnableManagedSpotTraining": args.use_spot,
        "StoppingCondition": {
            "MaxRuntimeInSeconds":        7200,   # 2 hours hard limit
            "MaxWaitTimeInSeconds":       86400,  # 24 hours max wait for Spot capacity
        },
        "EnableNetworkIsolation": False,  # Must be False to reach MLflow within VPC
        "EnableInterContainerTrafficEncryption": True,
        "Tags": [
            {"Key": "Environment", "Value": args.environment},
            {"Key": "Project",     "Value": "churn-platform"},
            {"Key": "Team",        "Value": "ml-platform"},
            {"Key": "CostCenter",  "Value": "ml-training"},
            {"Key": "ManagedBy",   "Value": "github-actions"},
            {"Key": "ModelType",   "Value": args.model_type},
            {"Key": "GitSha",      "Value": os.environ.get("GIT_SHA", "unknown")},
        ],
        "Environment": {
            "GIT_SHA":        os.environ.get("GIT_SHA", "unknown"),
            "ENVIRONMENT":    args.environment,
            # MLFLOW_TRACKING_URI also in hyperparams for training script access
            "MLFLOW_TRACKING_URI": os.environ.get(
                "MLFLOW_TRACKING_URI", "http://mlflow.mlops.svc.cluster.local:5000"
            ),
        },
    }

    # Add VPC config if set (training container needs to reach MLflow in-cluster)
    vpc_subnets        = os.environ.get("SAGEMAKER_VPC_SUBNETS", "").split(",")
    vpc_security_groups = os.environ.get("SAGEMAKER_VPC_SECURITY_GROUPS", "").split(",")
    if all(vpc_subnets) and all(vpc_security_groups):
        job_config["VpcConfig"] = {
            "Subnets":         [s.strip() for s in vpc_subnets if s.strip()],
            "SecurityGroupIds": [sg.strip() for sg in vpc_security_groups if sg.strip()],
        }

    log.info(f"Submitting training job: {job_name}")
    log.info(f"  Model type:    {args.model_type}")
    log.info(f"  Image:         {image_uri}")
    log.info(f"  Instance type: {INSTANCE_TYPES[args.model_type]}")
    log.info(f"  Spot enabled:  {args.use_spot}")
    log.info(f"  Output path:   {output_path}")

    response = sm_client.create_training_job(**job_config)
    log.info(f"Job ARN: {response['TrainingJobArn']}")
    return response


def wait_for_completion(sm_client, job_name: str) -> str:
    """
    Poll until job completes (or fails).
    Prints status updates every 30 seconds.
    Returns the final job status.
    """
    log.info(f"Waiting for job {job_name} to complete...")
    poll_interval = 30
    last_status   = None

    while True:
        desc   = sm_client.describe_training_job(TrainingJobName=job_name)
        status = desc["TrainingJobStatus"]

        if status != last_status:
            elapsed = desc.get("TrainingTimeInSeconds", 0)
            log.info(f"Status: {status} | Elapsed: {elapsed}s")
            last_status = status

        if status in ("Completed", "Failed", "Stopped"):
            if status == "Failed":
                reason = desc.get("FailureReason", "Unknown")
                log.error(f"Job failed: {reason}")
            break

        time.sleep(poll_interval)

    # Output the S3 URI of saved artifacts (used by evaluate step in GitHub Actions)
    if last_status == "Completed":
        model_uri = desc["ModelArtifacts"]["S3ModelArtifacts"]
        log.info(f"Model artifacts: {model_uri}")
        # Write to file so GitHub Actions step can read it
        Path("training_output.json").write_text(json.dumps({
            "job_name":    job_name,
            "model_s3_uri": model_uri,
            "status":       last_status,
        }))
        print(f"::set-output name=model_s3_uri::{model_uri}")
        print(f"::set-output name=job_name::{job_name}")

    return last_status


def main():
    args      = parse_args()
    sm_client = boto3.client("sagemaker", region_name=REGION)
    job_name  = build_job_name(args.model_type)

    try:
        submit_job(args, sm_client, job_name)
    except ClientError as e:
        log.error(f"Failed to submit training job: {e}")
        sys.exit(1)

    if not args.wait:
        log.info(f"Job submitted: {job_name} (not waiting — use --wait to block)")
        print(f"::set-output name=job_name::{job_name}")
        sys.exit(0)

    final_status = wait_for_completion(sm_client, job_name)
    sys.exit(0 if final_status == "Completed" else 1)


if __name__ == "__main__":
    main()
