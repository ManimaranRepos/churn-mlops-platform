"""
DAG: churn_feature_pipeline
Schedule: Daily at 02:00 UTC (after overnight Kinesis → Firehose → S3 data lands)

Orchestrates the full path from raw S3 events to training-ready feature vectors:

  raw S3 data
    → Glue: raw_to_curated   (dedup, normalise, MERGE into Iceberg)
    → Glue: feature_engineering  (7/30/90-day windows, churn label)
    → export train/val/test splits to S3
    → data validation (validate_data.py quality gates)
    → trigger training_pipeline DAG

WHY daily (not hourly)?
  Churn prediction is a daily snapshot model — churn doesn't happen within minutes.
  Training on a new snapshot daily is sufficient. Hourly would waste Glue DPUs.

WHY 02:00 UTC?
  Kinesis Firehose buffers up to 300s and flushes to S3.
  The Glue crawler is scheduled at 01:30 UTC to discover the new partition.
  By 02:00 all previous day's data is catalogued and ready.

SLA: The full pipeline must complete by 04:00 UTC (2 hours).
  If it runs past that, the model won't be updated before business hours.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
import pandas as pd
from airflow import DAG
from airflow.operators.python import PythonOperator, ShortCircuitOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sensors.s3_key_sensor import S3KeySensor
from airflow.utils.dates import days_ago

from common.aws_utils import (
    emit_dag_metric,
    get_latest_s3_partition,
    start_glue_job,
    trigger_glue_crawler,
    wait_for_glue_job,
)
from common.constants import (
    ARTIFACTS_BUCKET,
    ATHENA_WORKGROUP,
    AWS_REGION,
    ENVIRONMENT,
    FEATURES_S3_PREFIX,
    GLUE_CURATED_DATABASE,
    GLUE_CRAWLER_CURATED,
    GLUE_CRAWLER_RAW,
    GLUE_JOB_FEATURE_ENGINEERING,
    GLUE_JOB_RAW_TO_CURATED,
    GLUE_RAW_DATABASE,
    PROCESSED_BUCKET,
    PROJECT,
    RAW_BUCKET,
    TEST_S3_PATH,
    TRAIN_S3_PATH,
    VAL_S3_PATH,
)
from common.slack_notify import on_dag_failure, on_dag_success, on_sla_miss

log = logging.getLogger(__name__)

# ── DAG default args ───────────────────────────────────────────────────────────
DEFAULT_ARGS = {
    "owner":            "ml-platform",
    "depends_on_past":  False,         # Don't block today's run on yesterday's failure
    "retries":          2,
    "retry_delay":      timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "on_failure_callback": on_dag_failure,
    "email_on_failure": False,         # Using Slack callbacks instead
    "email_on_retry":   False,
}

# ── DAG definition ─────────────────────────────────────────────────────────────
with DAG(
    dag_id="churn_feature_pipeline",
    description="Raw S3 events → Glue ETL → Iceberg features → train/val/test splits",
    schedule_interval="0 2 * * *",     # 02:00 UTC daily
    start_date=days_ago(1),
    catchup=False,                     # Don't backfill missed runs
    max_active_runs=1,                 # One run at a time (no parallel ETL on same data)
    default_args=DEFAULT_ARGS,
    on_failure_callback=on_dag_failure,
    on_success_callback=on_dag_success,
    sla_miss_callback=on_sla_miss,
    tags=["data", "features", "glue", "iceberg"],
) as dag:

    # ── Task 1: Check raw data arrived ────────────────────────────────────────
    # WHY S3KeySensor (not just starting immediately)?
    # The Firehose writes data as it arrives. If the pipeline runs before
    # midnight data has flushed, we'd train on incomplete data.
    # The sensor waits up to 60 min for yesterday's partition to exist.
    wait_for_raw_data = S3KeySensor(
        task_id="wait_for_raw_data",
        bucket_name=RAW_BUCKET,
        bucket_key=(
            "events/year={{ macros.ds_format(ds, '%Y-%m-%d', '%Y') }}/"
            "month={{ macros.ds_format(ds, '%Y-%m-%d', '%m') }}/"
            "day={{ macros.ds_format(ds, '%Y-%m-%d', '%d') }}/"
        ),
        wildcard_match=True,
        poke_interval=120,           # Check every 2 min
        timeout=3600,                # Fail after 1h if data never arrives
        mode="reschedule",           # Release the slot between checks (saves resources)
        aws_conn_id="aws_default",
        deferrable=True,             # Async sensor (requires Triggerer)
    )

    # ── Task 2: Run raw → curated Glue job ────────────────────────────────────
    def run_raw_to_curated(**context) -> str:
        """
        Starts the raw_to_curated Glue job and stores the run ID in XCom.
        WHY store run ID in XCom (not just the result)?
        The next task polls Glue — it needs the run ID to call get_job_run().
        XCom is Airflow's inter-task communication mechanism.
        """
        execution_date = context["ds"]   # YYYY-MM-DD string
        run_id = start_glue_job(
            job_name=GLUE_JOB_RAW_TO_CURATED,
            arguments={
                "--execution_date":  execution_date,
                "--source_database": GLUE_RAW_DATABASE,
                "--target_database": GLUE_CURATED_DATABASE,
            },
            region=AWS_REGION,
            timeout_minutes=90,
        )
        return run_id  # Stored in XCom automatically (return value of PythonOperator)

    start_raw_to_curated = PythonOperator(
        task_id="start_raw_to_curated_job",
        python_callable=run_raw_to_curated,
        provide_context=True,
    )

    # ── Task 3: Wait for raw → curated to finish ──────────────────────────────
    def poll_raw_to_curated(**context) -> None:
        run_id = context["task_instance"].xcom_pull(
            task_ids="start_raw_to_curated_job"
        )
        final_state = wait_for_glue_job(
            job_name=GLUE_JOB_RAW_TO_CURATED,
            run_id=run_id,
            region=AWS_REGION,
        )
        emit_dag_metric("GlueJobCompleted", 1, dag_id="churn_feature_pipeline")
        log.info(f"raw_to_curated completed: {final_state}")

    wait_raw_to_curated = PythonOperator(
        task_id="wait_for_raw_to_curated",
        python_callable=poll_raw_to_curated,
        provide_context=True,
        execution_timeout=timedelta(hours=2),
        sla=timedelta(hours=1, minutes=30),  # Alert if this takes > 1.5h
    )

    # ── Task 4: Update curated Glue catalog ───────────────────────────────────
    # The Glue crawler discovers new Iceberg partitions so Athena can query them.
    def crawl_curated(**context) -> None:
        trigger_glue_crawler(GLUE_CRAWLER_CURATED, region=AWS_REGION)

    update_curated_catalog = PythonOperator(
        task_id="update_curated_catalog",
        python_callable=crawl_curated,
        provide_context=True,
    )

    # ── Task 5: Feature engineering Glue job ──────────────────────────────────
    def run_feature_engineering(**context) -> str:
        execution_date = context["ds"]
        run_id = start_glue_job(
            job_name=GLUE_JOB_FEATURE_ENGINEERING,
            arguments={
                "--execution_date":  execution_date,
                "--source_database": GLUE_CURATED_DATABASE,
                "--target_prefix":   f"s3://{PROCESSED_BUCKET}/features/raw/",
            },
            region=AWS_REGION,
            timeout_minutes=90,
        )
        return run_id

    start_feature_eng = PythonOperator(
        task_id="start_feature_engineering_job",
        python_callable=run_feature_engineering,
        provide_context=True,
    )

    def poll_feature_eng(**context) -> None:
        run_id = context["task_instance"].xcom_pull(
            task_ids="start_feature_engineering_job"
        )
        wait_for_glue_job(
            job_name=GLUE_JOB_FEATURE_ENGINEERING,
            run_id=run_id,
            region=AWS_REGION,
        )

    wait_feature_eng = PythonOperator(
        task_id="wait_for_feature_engineering",
        python_callable=poll_feature_eng,
        provide_context=True,
        execution_timeout=timedelta(hours=2),
        sla=timedelta(hours=1, minutes=30),
    )

    # ── Task 6: Export train/val/test splits to S3 ────────────────────────────
    # WHY split here (in Airflow) instead of inside the training script?
    #   - Both XGBoost and PyTorch training jobs need the SAME split.
    #     If each script splits independently, they'd use different test sets
    #     making model comparison invalid.
    #   - The split is deterministic (stratified, fixed seed) but we compute it
    #     once here and hand both jobs the same pre-split S3 paths.
    def export_train_val_test_splits(**context) -> dict:
        """
        Read the feature snapshot from S3, split into train/val/test,
        and write each split to a separate S3 prefix.
        """
        import awswrangler as wr
        from sklearn.model_selection import train_test_split

        execution_date = context["ds"]
        source_path    = f"s3://{PROCESSED_BUCKET}/features/raw/{execution_date}/"

        log.info(f"Loading features from: {source_path}")
        df = wr.s3.read_parquet(path=source_path)
        log.info(f"Loaded {len(df):,} rows | Churn rate: {df['is_churned'].mean():.1%}")

        # Three-way stratified split (same logic as data_loader.py)
        X_temp, df_test = train_test_split(
            df, test_size=0.20, stratify=df["is_churned"], random_state=42
        )
        df_train, df_val = train_test_split(
            X_temp, test_size=0.125, stratify=X_temp["is_churned"], random_state=42
        )

        # Write splits to versioned S3 paths
        splits = {
            "train":      (df_train, f"s3://{PROCESSED_BUCKET}/features/train/{execution_date}/"),
            "validation": (df_val,   f"s3://{PROCESSED_BUCKET}/features/validation/{execution_date}/"),
            "test":       (df_test,  f"s3://{PROCESSED_BUCKET}/features/test/{execution_date}/"),
        }

        for split_name, (split_df, s3_path) in splits.items():
            wr.s3.to_parquet(
                df=split_df,
                path=s3_path,
                dataset=True,       # Writes as a proper dataset (not a single file)
                mode="overwrite",
                compression="snappy",
            )
            log.info(
                f"Wrote {split_name}: {len(split_df):,} rows "
                f"({split_df['is_churned'].mean():.1%} churn) → {s3_path}"
            )

        split_info = {
            "execution_date": execution_date,
            "train_path":     splits["train"][1],
            "val_path":       splits["validation"][1],
            "test_path":      splits["test"][1],
            "train_rows":     len(df_train),
            "val_rows":       len(df_val),
            "test_rows":      len(df_test),
            "churn_rate":     float(df["is_churned"].mean()),
        }
        emit_dag_metric("FeatureRows", float(len(df)), dag_id="churn_feature_pipeline")
        return split_info  # Pushed to XCom for downstream tasks + training DAG

    export_splits = PythonOperator(
        task_id="export_train_val_test_splits",
        python_callable=export_train_val_test_splits,
        provide_context=True,
        execution_timeout=timedelta(minutes=30),
    )

    # ── Task 7: Validate features ──────────────────────────────────────────────
    # WHY validate AFTER splitting (not before)?
    # We validate the full feature set first (row counts, null rates, freshness).
    # If validation fails, we stop before triggering a training job that would
    # just fail at the quality gate step — faster feedback loop.
    def run_data_validation(**context) -> None:
        """
        Call validate_data.py checks inline (reuses the same logic
        without requiring a subprocess call into a separate container).
        """
        import subprocess
        import sys

        split_info = context["task_instance"].xcom_pull(
            task_ids="export_train_val_test_splits"
        )
        train_path = split_info["train_path"]

        result = subprocess.run(
            [
                sys.executable,
                "/opt/airflow/dags/../../../ml/validation/validate_data.py",
                "--input-s3", train_path,
                "--min-rows",       "500",
                "--max-null-rate",  "0.5",
                "--min-churn-rate", "0.03",
                "--max-churn-rate", "0.30",
                "--max-data-age-hours", "48",
                "--output-file",    "/tmp/validation_result.json",
            ],
            capture_output=True,
            text=True,
        )

        log.info(result.stdout)
        if result.returncode != 0:
            log.error(result.stderr)
            raise RuntimeError(
                f"Data validation failed:\n{result.stderr}"
            )

        with open("/tmp/validation_result.json") as f:
            validation = json.load(f)

        log.info(f"Validation passed | Stats: {validation.get('stats')}")
        emit_dag_metric("ValidationPassed", 1, dag_id="churn_feature_pipeline")

    validate_features = PythonOperator(
        task_id="validate_features",
        python_callable=run_data_validation,
        provide_context=True,
        execution_timeout=timedelta(minutes=20),
    )

    # ── Task 8: Gate — only trigger training if validation passed ─────────────
    def check_validation_passed(**context) -> bool:
        """ShortCircuitOperator: returns False to skip remaining tasks on failure."""
        try:
            with open("/tmp/validation_result.json") as f:
                result = json.load(f)
            passed = result.get("passed", False)
            if not passed:
                log.warning(f"Validation failed: {result.get('failures')}")
            return passed
        except FileNotFoundError:
            return False  # Validation didn't run — don't proceed

    validation_gate = ShortCircuitOperator(
        task_id="validation_gate",
        python_callable=check_validation_passed,
        provide_context=True,
    )

    # ── Task 9: Trigger training pipeline ─────────────────────────────────────
    # WHY TriggerDagRunOperator (not a direct function call)?
    # The training pipeline is a separate DAG because:
    #   - It can be triggered independently (e.g., for retraining without new data)
    #   - It has its own SLA, retry policy, and failure notifications
    #   - The feature pipeline should not wait hours for training to finish
    #     (it completes as soon as training is triggered)
    trigger_training = TriggerDagRunOperator(
        task_id="trigger_training_pipeline",
        trigger_dag_id="churn_training_pipeline",
        conf={
            "execution_date": "{{ ds }}",
            "triggered_by":   "churn_feature_pipeline",
        },
        wait_for_completion=False,   # Don't block — feature pipeline is done
        reset_dag_run=True,
    )

    # ── Task ordering ──────────────────────────────────────────────────────────
    (
        wait_for_raw_data
        >> start_raw_to_curated
        >> wait_raw_to_curated
        >> update_curated_catalog
        >> start_feature_eng
        >> wait_feature_eng
        >> export_splits
        >> validate_features
        >> validation_gate
        >> trigger_training
    )
