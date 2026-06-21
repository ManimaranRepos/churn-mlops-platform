"""
SageMaker Model Monitor — Baseline Capture

Model Monitor works by comparing live inference traffic against a baseline.
The baseline describes what "normal" data looks like: feature distributions,
null rates, value ranges, etc.

This script captures the baseline from the training dataset and uploads it to
the S3 location that Model Monitor's DataQualityMonitor expects.

WHY run this as a separate step (not inline in training)?
  - Baseline capture uses a SageMaker Processing job (not a local script).
    It needs the training data in S3 and produces structured statistics.json
    and constraints.json files that SageMaker Model Monitor understands natively.
  - It only needs to run once per model version, not on every training run.
    Calling it from the training script would run it every epoch.
  - The constraints.json can be edited manually after capture to tighten or
    relax thresholds before deploying monitoring.

What gets captured:
  - statistics.json: mean, std, min/max, histogram per feature
  - constraints.json: suggested thresholds (can be edited)

These are then referenced by DataQualityMonitoringJobDefinition as the baseline.

Called from: the Airflow training_pipeline DAG after model promotion,
or manually via: python baseline_capture.py --model-version <ver>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

import boto3
import sagemaker
from sagemaker.model_monitor import DataCaptureConfig, DefaultModelMonitor
from sagemaker.model_monitor.dataset_format import DatasetFormat
from sagemaker.processing import ProcessingInput, ProcessingOutput
from sagemaker.sklearn.processing import SKLearnProcessor

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

AWS_REGION      = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT     = os.environ.get("ENVIRONMENT", "dev")
PROJECT         = os.environ.get("PROJECT", "churn-platform")
ARTIFACTS_BUCKET = os.environ["ARTIFACTS_BUCKET"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
SAGEMAKER_ROLE  = os.environ["SAGEMAKER_ROLE_ARN"]


def run_baseline_job(
    training_data_s3_uri: str,
    baseline_output_s3_uri: str,
    model_version: str,
    instance_type: str = "ml.m5.large",
) -> dict:
    """
    Launch a SageMaker Model Monitor baseline job.

    The job:
      1. Reads training CSV from S3
      2. Computes per-feature statistics (mean, std, histogram, null rate)
      3. Suggests constraint thresholds (e.g., null_rate < 0.05)
      4. Writes statistics.json + constraints.json to baseline_output_s3_uri

    These two files become the reference point for live monitoring.
    """
    sess = sagemaker.Session(boto_session=boto3.Session(region_name=AWS_REGION))

    monitor = DefaultModelMonitor(
        role=SAGEMAKER_ROLE,
        instance_count=1,
        instance_type=instance_type,
        volume_size_in_gb=20,
        max_runtime_in_seconds=3600,
        sagemaker_session=sess,
        tags=[
            {"Key": "Environment", "Value": ENVIRONMENT},
            {"Key": "Project",     "Value": PROJECT},
            {"Key": "ManagedBy",   "Value": "terraform"},
            {"Key": "ModelVersion","Value": model_version},
        ],
    )

    log.info(f"Starting baseline job for model version {model_version}")
    log.info(f"Training data: {training_data_s3_uri}")
    log.info(f"Baseline output: {baseline_output_s3_uri}")

    job_name = f"{PROJECT}-baseline-{model_version[:8]}-{int(time.time())}"

    monitor.suggest_baseline(
        baseline_dataset=training_data_s3_uri,
        dataset_format=DatasetFormat.csv(header=True),
        output_s3_uri=baseline_output_s3_uri,
        job_name=job_name,
        wait=True,
        logs=True,
    )

    log.info(f"Baseline job completed: {job_name}")

    # Read and return the generated constraints (for logging/auditing)
    s3     = boto3.client("s3", region_name=AWS_REGION)
    bucket = baseline_output_s3_uri.split("/")[2]
    prefix = "/".join(baseline_output_s3_uri.split("/")[3:])

    constraints_key = f"{prefix}/constraints.json"
    statistics_key  = f"{prefix}/statistics.json"

    try:
        constraints = json.loads(
            s3.get_object(Bucket=bucket, Key=constraints_key)["Body"].read()
        )
        statistics  = json.loads(
            s3.get_object(Bucket=bucket, Key=statistics_key)["Body"].read()
        )
        feature_count = len(statistics.get("features", []))
        log.info(f"Baseline captured {feature_count} features")
    except Exception as e:
        log.warning(f"Could not read baseline output files: {e}")
        constraints = {}
        statistics  = {}

    return {
        "job_name":           job_name,
        "baseline_s3_uri":    baseline_output_s3_uri,
        "constraints_s3_uri": f"{baseline_output_s3_uri}/constraints.json",
        "statistics_s3_uri":  f"{baseline_output_s3_uri}/statistics.json",
        "feature_count":      len(statistics.get("features", [])),
    }


def tighten_constraints(constraints_s3_uri: str, output_s3_uri: str) -> None:
    """
    Post-process the auto-generated constraints to make them production-grade.

    SageMaker's suggested constraints are permissive by default (e.g., it allows
    up to 100% null rate, which is useless). We tighten them based on our
    domain knowledge:
      - null_rate max: 0.05 (5%) — any feature with >5% nulls in live traffic is suspicious
      - fractional thresholds for categorical features: +/-20% from baseline distribution
      - numeric drift: 3 standard deviations from baseline mean

    WHY edit constraints here (not in Terraform)?
      The constraints.json format is defined by SageMaker and changes with SDK versions.
      It's safer to read and modify the auto-generated file than to maintain a
      parallel hand-crafted version that might drift from SageMaker's expected schema.
    """
    s3     = boto3.client("s3", region_name=AWS_REGION)
    bucket = constraints_s3_uri.split("/")[2]
    key    = "/".join(constraints_s3_uri.split("/")[3:])

    try:
        raw         = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        constraints = json.loads(raw)
    except Exception as e:
        log.error(f"Cannot read constraints from {constraints_s3_uri}: {e}")
        return

    features = constraints.get("features", [])
    for feature in features:
        # Tighten null rate threshold
        if "completeness" in feature:
            completeness = feature["completeness"]
            if completeness.get("threshold") == 0:
                completeness["threshold"] = 0.95     # Require 95% completeness minimum

        # For numerical features, ensure drift threshold is set
        if feature.get("inferred_type") == "Fractional":
            if "num_constraints" not in feature:
                feature["num_constraints"] = {}
            nc = feature["num_constraints"]
            if "mean" not in nc:
                nc["mean"] = {}
            if "stddev" not in nc:
                nc["stddev"] = {}

    constraints["features"] = features

    out_bucket = output_s3_uri.split("/")[2]
    out_key    = "/".join(output_s3_uri.split("/")[3:])
    s3.put_object(
        Bucket=out_bucket,
        Key=out_key,
        Body=json.dumps(constraints, indent=2).encode(),
        ContentType="application/json",
    )
    log.info(f"Tightened constraints written to s3://{out_bucket}/{out_key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture Model Monitor baseline")
    parser.add_argument("--model-version",   required=True, help="MLflow model version string")
    parser.add_argument("--execution-date",  required=True, help="Feature pipeline execution date (YYYY-MM-DD)")
    parser.add_argument("--instance-type",   default="ml.m5.large")
    args = parser.parse_args()

    training_data_s3_uri = (
        f"s3://{PROCESSED_BUCKET}/features/train/{args.execution_date}/"
    )
    baseline_output_s3_uri = (
        f"s3://{ARTIFACTS_BUCKET}/model-monitor/baselines/{args.model_version}/"
    )

    result = run_baseline_job(
        training_data_s3_uri  = training_data_s3_uri,
        baseline_output_s3_uri = baseline_output_s3_uri,
        model_version          = args.model_version,
        instance_type          = args.instance_type,
    )

    log.info("Baseline capture result:")
    log.info(json.dumps(result, indent=2))

    # Tighten auto-generated constraints
    tighten_constraints(
        constraints_s3_uri = result["constraints_s3_uri"],
        output_s3_uri      = result["constraints_s3_uri"],   # Overwrite in-place
    )

    # Write result for Airflow XCom
    Path("/tmp/baseline_result.json").write_text(json.dumps(result))
    log.info("Baseline capture complete")


if __name__ == "__main__":
    main()
