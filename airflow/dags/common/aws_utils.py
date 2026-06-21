"""
AWS utility functions used across DAGs.
All boto3 calls go through these helpers so error handling and retry logic
are consistent across every DAG (not duplicated in each task function).
"""

import logging
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

# ── Glue helpers ───────────────────────────────────────────────────────────────

def start_glue_job(
    job_name: str,
    arguments: Optional[dict] = None,
    region: str = "us-east-1",
    max_retries: int = 0,
    timeout_minutes: int = 120,
) -> str:
    """
    Start a Glue job and return the run ID.
    Does NOT wait — the calling task uses wait_for_glue_job() to poll.

    WHY split start and wait into separate functions?
    Airflow tasks hold a Python thread for their entire duration.
    If we block inside start_glue_job, an XGBoost training job taking 2h
    holds an Airflow task slot the whole time. Separate start+poll tasks
    are more Airflow-idiomatic (each poll is a lightweight task).
    """
    client = boto3.client("glue", region_name=region)
    run_args = {
        "--job-bookmark-option": "job-bookmark-enable",  # Only process new data
        **(arguments or {}),
    }

    response = client.start_job_run(
        JobName=job_name,
        Arguments=run_args,
        MaxCapacity=None,  # Uses job default DPU
        Timeout=timeout_minutes,
    )
    run_id = response["JobRunId"]
    log.info(f"Started Glue job '{job_name}' — run ID: {run_id}")
    return run_id


def wait_for_glue_job(
    job_name: str,
    run_id: str,
    poll_interval_seconds: int = 30,
    region: str = "us-east-1",
) -> str:
    """
    Poll until a Glue job run completes.
    Returns the final state string.
    Raises RuntimeError if the job failed or timed out.
    """
    client = boto3.client("glue", region_name=region)
    terminal_states = {"SUCCEEDED", "FAILED", "ERROR", "STOPPED", "TIMEOUT"}

    while True:
        response = client.get_job_run(JobName=job_name, RunId=run_id)
        run      = response["JobRun"]
        state    = run["JobRunState"]
        elapsed  = run.get("ExecutionTime", 0)

        log.info(f"Glue job '{job_name}' [{run_id}] — state: {state} | elapsed: {elapsed}s")

        if state in terminal_states:
            if state != "SUCCEEDED":
                error_msg = run.get("ErrorMessage", "No error message")
                raise RuntimeError(f"Glue job '{job_name}' {state}: {error_msg}")
            return state

        time.sleep(poll_interval_seconds)


def trigger_glue_crawler(crawler_name: str, region: str = "us-east-1") -> None:
    """Start a Glue crawler and wait for it to complete."""
    client = boto3.client("glue", region_name=region)
    client.start_crawler(Name=crawler_name)
    log.info(f"Started Glue crawler: {crawler_name}")

    while True:
        response = client.get_crawler(Name=crawler_name)
        state    = response["Crawler"]["State"]
        log.info(f"Crawler '{crawler_name}' state: {state}")
        if state == "READY":
            break
        if state == "STOPPING":
            time.sleep(10)
            continue
        time.sleep(30)


# ── SageMaker helpers ──────────────────────────────────────────────────────────

def submit_sagemaker_training_job(
    job_name: str,
    image_uri: str,
    role_arn: str,
    input_data_config: list,
    output_s3_path: str,
    hyperparameters: dict,
    instance_type: str = "ml.m5.xlarge",
    use_spot: bool = True,
    vpc_config: Optional[dict] = None,
    region: str = "us-east-1",
    artifacts_bucket: str = "",
    environment: str = "dev",
    project: str = "churn-platform",
) -> str:
    """
    Submit a SageMaker Training Job. Returns the job name (same as input).
    WHY return job_name instead of ARN?
    SageMaker APIs use job NAME for all follow-up calls (describe, logs, etc.)
    The ARN is only needed for IAM and resource policies.
    """
    client = boto3.client("sagemaker", region_name=region)

    job_config: dict = {
        "TrainingJobName": job_name,
        "RoleArn": role_arn,
        "AlgorithmSpecification": {
            "TrainingImage": image_uri,
            "TrainingInputMode": "File",
            "EnableSageMakerMetricsTimeSeries": True,
            "MetricDefinitions": [
                {"Name": "val:auc",       "Regex": r"val:auc: ([0-9\.]+)"},
                {"Name": "val:f1",        "Regex": r"val:f1: ([0-9\.]+)"},
                {"Name": "val:precision", "Regex": r"val:precision: ([0-9\.]+)"},
                {"Name": "val:recall",    "Regex": r"val:recall: ([0-9\.]+)"},
            ],
        },
        "HyperParameters":  {k: str(v) for k, v in hyperparameters.items()},
        "InputDataConfig":  input_data_config,
        "OutputDataConfig": {
            "S3OutputPath": output_s3_path,
        },
        "ResourceConfig": {
            "InstanceType":   instance_type,
            "InstanceCount":  1,
            "VolumeSizeInGB": 50,
        },
        "CheckpointConfig": {
            "S3Uri": f"s3://{artifacts_bucket}/sagemaker/checkpoints/{job_name}/",
        },
        "EnableManagedSpotTraining": use_spot,
        "StoppingCondition": {
            "MaxRuntimeInSeconds":   7200,
            "MaxWaitTimeInSeconds":  86400,
        },
        "EnableInterContainerTrafficEncryption": True,
        "Tags": [
            {"Key": "Environment", "Value": environment},
            {"Key": "Project",     "Value": project},
            {"Key": "ManagedBy",   "Value": "airflow"},
        ],
    }

    if vpc_config:
        job_config["VpcConfig"] = vpc_config

    client.create_training_job(**job_config)
    log.info(f"Submitted SageMaker training job: {job_name}")
    return job_name


def wait_for_sagemaker_job(
    job_name: str,
    poll_interval_seconds: int = 60,
    region: str = "us-east-1",
) -> dict:
    """
    Poll until a SageMaker training job completes.
    Returns the final describe response (contains model artifact URI).
    Raises RuntimeError on failure.
    """
    client = boto3.client("sagemaker", region_name=region)
    terminal_states = {"Completed", "Failed", "Stopped"}

    while True:
        desc    = client.describe_training_job(TrainingJobName=job_name)
        status  = desc["TrainingJobStatus"]
        elapsed = desc.get("TrainingTimeInSeconds", 0)

        log.info(f"SageMaker job '{job_name}' — status: {status} | elapsed: {elapsed}s")

        if status in terminal_states:
            if status != "Completed":
                reason = desc.get("FailureReason", "unknown")
                raise RuntimeError(f"SageMaker job '{job_name}' {status}: {reason}")
            return desc

        time.sleep(poll_interval_seconds)


# ── S3 helpers ─────────────────────────────────────────────────────────────────

def check_s3_prefix_exists(bucket: str, prefix: str, region: str = "us-east-1") -> bool:
    """Return True if at least one object exists under the given S3 prefix."""
    client = boto3.client("s3", region_name=region)
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return response.get("KeyCount", 0) > 0


def get_latest_s3_partition(
    bucket: str,
    prefix: str,
    region: str = "us-east-1",
) -> Optional[str]:
    """
    Return the lexicographically latest 'prefix' in the given S3 path.
    Assumes Hive-style partitioning: year=YYYY/month=MM/day=DD/
    Returns full s3:// path, or None if no partitions exist.
    """
    client = boto3.client("s3", region_name=region)
    response = client.list_objects_v2(
        Bucket=bucket, Prefix=prefix, Delimiter="/"
    )
    prefixes = [cp["Prefix"] for cp in response.get("CommonPrefixes", [])]
    if not prefixes:
        return None
    latest = sorted(prefixes)[-1]
    return f"s3://{bucket}/{latest}"


# ── CloudWatch helpers ─────────────────────────────────────────────────────────

def emit_dag_metric(
    metric_name: str,
    value: float,
    dag_id: str,
    unit: str = "Count",
    region: str = "us-east-1",
) -> None:
    """Emit a custom metric to CloudWatch from a DAG task."""
    client = boto3.client("cloudwatch", region_name=region)
    client.put_metric_data(
        Namespace="ChurnPlatform/Airflow",
        MetricData=[{
            "MetricName": metric_name,
            "Value":      value,
            "Unit":       unit,
            "Dimensions": [{"Name": "DAG", "Value": dag_id}],
        }],
    )
