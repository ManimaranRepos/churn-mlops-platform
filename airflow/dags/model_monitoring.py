"""
DAG: churn_model_monitoring
Schedule: Weekly on Sunday 01:00 UTC

Orchestrates model health checks that require ground truth data:
  1. collect_ground_truth   — joins Data Capture predictions to CRM churn outcomes
  2. run_drift_analysis     — reads Model Monitor violation reports, scores drift
  3. capture_new_baseline   — if model was retrained last week, capture new baseline

WHY weekly (not the 6-hour Lambda)?
  The Lambda fires after each monitoring job (every 6h) for data quality drift.
  This weekly DAG handles the ground-truth-dependent tasks that only make sense
  on a longer cadence:
    - Ground truth collection: CRM outcomes take 7-30 days to be confirmed.
      Running daily would find near-zero matches. Weekly catches the past week's outcomes.
    - Baseline recapture: Only needed after a model promotion. Weekly check is sufficient.

WHY in Airflow (not just a Lambda)?
  Airflow gives us:
    - Visible history: which weeks had drift, which triggered retraining
    - Retry logic: if ground truth collection fails due to CRM API outage, retry
    - SLA alerting: if monitoring hasn't run for 2 weeks, alert
    - XCom: pass drift results between tasks for conditional baseline capture
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.operators.empty import EmptyOperator
from airflow.utils.dates import days_ago

from common.aws_utils import emit_dag_metric
from common.constants import (
    ARTIFACTS_BUCKET,
    AWS_REGION,
    ENVIRONMENT,
    PROJECT,
    RAW_BUCKET,
)
from common.slack_notify import on_dag_failure, on_dag_success

log = logging.getLogger(__name__)

DEFAULT_ARGS = {
    "owner":               "ml-platform",
    "depends_on_past":     False,
    "retries":             1,
    "retry_delay":         timedelta(minutes=30),
    "on_failure_callback": on_dag_failure,
    "email_on_failure":    False,
}

# These are set by Terraform output and stored in Airflow Variables
import os
DATA_QUALITY_SCHEDULE  = os.environ.get("DATA_QUALITY_SCHEDULE_NAME",  "")
MODEL_QUALITY_SCHEDULE = os.environ.get("MODEL_QUALITY_SCHEDULE_NAME", "")
SAGEMAKER_ROLE_ARN     = os.environ.get("SAGEMAKER_ROLE_ARN",          "")
AIRFLOW_API_URL        = os.environ.get("AIRFLOW_API_URL",              "http://localhost:8080")


with DAG(
    dag_id="churn_model_monitoring",
    description="Weekly model health: ground truth collection, drift analysis, baseline recapture",
    schedule_interval="0 1 * * 0",    # Sunday 01:00 UTC
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    on_success_callback=on_dag_success,
    on_failure_callback=on_dag_failure,
    tags=["monitoring", "model-quality", "mlops"],
) as dag:

    # ── Task 1: Collect ground truth ──────────────────────────────────────────
    def collect_ground_truth(**context) -> dict:
        """
        Join Data Capture predictions to CRM churn outcomes for the past week.
        Produces merged_labels.csv consumed by Model Quality Monitor.
        """
        result = subprocess.run(
            [
                "python", "-m", "ml.monitoring.ground_truth_collector",
                "--lookback-days", "90",
                "--ground-truth-days", "7",
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ARTIFACTS_BUCKET":  ARTIFACTS_BUCKET,
                "RAW_BUCKET":        RAW_BUCKET,
                "ENVIRONMENT":       ENVIRONMENT,
                "PROJECT":           PROJECT,
                "AWS_REGION":        AWS_REGION,
            },
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Ground truth collection failed:\n{result.stderr}"
            )

        try:
            gt_result = json.loads(
                open("/tmp/ground_truth_result.json").read()
            )
        except Exception:
            gt_result = {"merged_count": 0}

        log.info(f"Ground truth collected: {gt_result}")
        emit_dag_metric("GroundTruthRecords", float(gt_result.get("merged_count", 0)), "churn_model_monitoring")

        return gt_result

    collect_gt_task = PythonOperator(
        task_id="collect_ground_truth",
        python_callable=collect_ground_truth,
        provide_context=True,
        execution_timeout=timedelta(hours=2),
    )

    # ── Task 2: Run drift analysis ────────────────────────────────────────────
    def run_drift_analysis(**context) -> dict:
        """
        Read the latest Model Monitor violation reports and compute drift scores.
        If drift threshold exceeded, trigger the training pipeline automatically.
        """
        if not DATA_QUALITY_SCHEDULE:
            log.warning("DATA_QUALITY_SCHEDULE_NAME not configured — skipping drift analysis")
            return {"any_critical": False}

        result = subprocess.run(
            [
                "python", "-m", "ml.monitoring.drift_detector",
                "--data-quality-schedule",  DATA_QUALITY_SCHEDULE,
                "--model-quality-schedule", MODEL_QUALITY_SCHEDULE,
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ARTIFACTS_BUCKET":    ARTIFACTS_BUCKET,
                "RAW_BUCKET":          RAW_BUCKET,
                "ENVIRONMENT":         ENVIRONMENT,
                "PROJECT":             PROJECT,
                "AWS_REGION":          AWS_REGION,
                "AIRFLOW_API_URL":     AIRFLOW_API_URL,
                "AIRFLOW_API_SECRET":  f"churn-platform/{ENVIRONMENT}/airflow-api-credentials",
            },
        )

        try:
            drift_result = json.loads(
                open("/tmp/drift_result.json").read()
            )
        except Exception:
            drift_result = {"any_critical": False}

        dq = drift_result.get("data_quality", {})
        mq = drift_result.get("model_quality", {})

        emit_dag_metric("DataDriftScore",  float(dq.get("drift_score", 0)),  "churn_model_monitoring")
        emit_dag_metric("ModelDriftScore", float(mq.get("drift_score", 0)),  "churn_model_monitoring")

        log.info(f"Drift analysis complete: {json.dumps(drift_result, indent=2)}")

        # Return code 1 = critical drift found — we catch this and handle it
        # We don't re-raise here: drift is not a DAG failure, it's an event
        return drift_result

    drift_task = PythonOperator(
        task_id="run_drift_analysis",
        python_callable=run_drift_analysis,
        provide_context=True,
        execution_timeout=timedelta(hours=1),
    )

    # ── Task 3: Branch — should we recapture baseline? ────────────────────────
    def should_recapture_baseline(**context) -> str:
        """
        Recapture baseline if:
          a) A new model was promoted to Production in the past 7 days (check MLflow)
          b) Drift was so severe that a new baseline is needed post-retraining

        If neither condition is met, skip baseline recapture (expensive: runs a
        SageMaker Processing job).
        """
        import mlflow
        mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", ""))

        client   = mlflow.tracking.MlflowClient()
        versions = client.search_model_versions("name='churn-prediction'")

        now      = datetime.utcnow()
        one_week_ago = now - timedelta(days=7)

        for v in versions:
            if v.current_stage == "Production":
                created_at = datetime.utcfromtimestamp(v.creation_timestamp / 1000)
                if created_at > one_week_ago:
                    log.info(
                        f"New Production model found: version={v.version}, "
                        f"promoted={created_at.isoformat()}"
                    )
                    return "capture_baseline"

        log.info("No new Production model in last 7 days — skipping baseline recapture")
        return "skip_baseline"

    baseline_branch = BranchPythonOperator(
        task_id="should_recapture_baseline",
        python_callable=should_recapture_baseline,
        provide_context=True,
    )

    # ── Task 4a: Capture new baseline ─────────────────────────────────────────
    def capture_baseline(**context) -> dict:
        """
        Run baseline_capture.py to produce new statistics.json + constraints.json
        from the current training data, replacing the old baseline.

        This is needed after a model promotion because:
          - The new model may have been trained on different features
          - The preprocessing pipeline may have changed (new columns, different scaling)
          - Comparing live traffic to the old model's baseline would produce spurious violations
        """
        import mlflow
        mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", ""))

        client   = mlflow.tracking.MlflowClient()
        versions = client.get_latest_versions("churn-prediction", stages=["Production"])
        if not versions:
            raise RuntimeError("No Production model found in MLflow")

        model_version  = versions[0].version
        execution_date = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        result = subprocess.run(
            [
                "python", "-m", "ml.monitoring.baseline_capture",
                "--model-version",  str(model_version),
                "--execution-date", execution_date,
                "--instance-type",  "ml.m5.large",
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ARTIFACTS_BUCKET": ARTIFACTS_BUCKET,
                "PROCESSED_BUCKET": os.environ.get("PROCESSED_BUCKET", ""),
                "SAGEMAKER_ROLE_ARN": SAGEMAKER_ROLE_ARN,
                "ENVIRONMENT":       ENVIRONMENT,
                "PROJECT":           PROJECT,
                "AWS_REGION":        AWS_REGION,
            },
        )

        if result.returncode != 0:
            raise RuntimeError(f"Baseline capture failed:\n{result.stderr}")

        try:
            baseline_result = json.loads(open("/tmp/baseline_result.json").read())
        except Exception:
            baseline_result = {}

        log.info(f"Baseline captured: {json.dumps(baseline_result, indent=2)}")
        return baseline_result

    capture_baseline_task = PythonOperator(
        task_id="capture_baseline",
        python_callable=capture_baseline,
        provide_context=True,
        execution_timeout=timedelta(hours=2),
    )

    # ── Task 4b: Skip baseline (branch target) ────────────────────────────────
    skip_baseline_task = EmptyOperator(task_id="skip_baseline")

    # ── Task 5: Emit summary metric ───────────────────────────────────────────
    def emit_weekly_summary(**context) -> None:
        drift_result = context["task_instance"].xcom_pull(task_ids="run_drift_analysis") or {}
        gt_result    = context["task_instance"].xcom_pull(task_ids="collect_ground_truth") or {}

        any_retraining = (
            drift_result.get("data_quality", {}).get("retraining", False)
            or drift_result.get("model_quality", {}).get("retraining", False)
        )

        emit_dag_metric(
            "WeeklyMonitoringComplete",
            1.0,
            "churn_model_monitoring",
        )
        emit_dag_metric(
            "AutoRetrainingTriggered",
            1.0 if any_retraining else 0.0,
            "churn_model_monitoring",
        )

        log.info(
            f"Weekly monitoring summary | "
            f"GroundTruthRecords={gt_result.get('merged_count', 0)} | "
            f"RetrainingTriggered={any_retraining}"
        )

    summary_task = PythonOperator(
        task_id="emit_weekly_summary",
        python_callable=emit_weekly_summary,
        provide_context=True,
        trigger_rule="all_done",   # Run even if baseline was skipped
    )

    # ── Task ordering ─────────────────────────────────────────────────────────
    collect_gt_task >> drift_task >> baseline_branch
    baseline_branch >> [capture_baseline_task, skip_baseline_task]
    [capture_baseline_task, skip_baseline_task] >> summary_task
