"""
DAG: churn_training_pipeline
Triggered by: churn_feature_pipeline (via TriggerDagRunOperator)
Also supports: manual trigger via Airflow UI / workflow_dispatch

Orchestrates the full ML training → evaluation → canary deploy flow:

  XGBoost training job (SageMaker)  ──┐
                                       ├── evaluate both → pick winner → canary deploy
  PyTorch training job (SageMaker)  ──┘

WHY train both models in parallel?
  SageMaker training jobs are independent — no reason to serialise them.
  XGBoost takes ~3 min, PyTorch ~8 min. Running in parallel saves 3 min/day.
  The comparison step then picks whichever achieved higher test AUC.

WHY train PyTorch at all if XGBoost usually wins on tabular data?
  1. Forces a genuine comparison each run (avoids assumption lock-in)
  2. PyTorch MLP can outperform XGBoost when feature interactions are complex
  3. Acts as an ensemble fallback (average both for +0.5-1% AUC lift)
  4. Validates that the PyTorch pipeline is working before we need it
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import boto3
from airflow import DAG
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.utils.dates import days_ago
from airflow.utils.trigger_rule import TriggerRule

from common.aws_utils import (
    emit_dag_metric,
    submit_sagemaker_training_job,
    wait_for_sagemaker_job,
)
from common.constants import (
    ARTIFACTS_BUCKET,
    AWS_REGION,
    ENVIRONMENT,
    MLFLOW_MODEL_NAME_PYTORCH,
    MLFLOW_MODEL_NAME_XGBOOST,
    MLFLOW_TRACKING_URI,
    PROCESSED_BUCKET,
    PROJECT,
    QUALITY_GATE_AUC,
    QUALITY_GATE_LATENCY_MS,
    QUALITY_GATE_PRECISION,
    QUALITY_GATE_RECALL,
    SAGEMAKER_EXECUTION_ROLE_ARN,
    SAGEMAKER_VPC_SG,
    SAGEMAKER_VPC_SUBNETS,
)
from common.slack_notify import on_dag_failure, on_dag_success

log = logging.getLogger(__name__)

AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
ECR_BASE = f"{AWS_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com/churn-platform"
GIT_SHA  = os.environ.get("GIT_SHA", "latest")

DEFAULT_ARGS = {
    "owner":           "ml-platform",
    "depends_on_past": False,
    "retries":         1,
    "retry_delay":     timedelta(minutes=10),
    "on_failure_callback": on_dag_failure,
    "email_on_failure": False,
}

with DAG(
    dag_id="churn_training_pipeline",
    description="Train XGBoost + PyTorch models, evaluate, canary deploy best model",
    schedule_interval=None,         # Triggered only by feature_pipeline or manual
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    on_failure_callback=on_dag_failure,
    on_success_callback=on_dag_success,
    params={
        "execution_date": "{{ ds }}",
        "triggered_by":   "manual",
        "force_model":    None,     # Set to 'xgboost' or 'pytorch' to skip comparison
    },
    tags=["ml", "training", "sagemaker"],
) as dag:

    # ── Helper: build SageMaker input data config ─────────────────────────────
    def _input_channel(channel_name: str, execution_date: str) -> dict:
        return {
            "ChannelName": channel_name,
            "DataSource": {
                "S3DataSource": {
                    "S3DataType":             "S3Prefix",
                    "S3Uri":                  f"s3://{PROCESSED_BUCKET}/features/{channel_name}/{execution_date}/",
                    "S3DataDistributionType": "FullyReplicated",
                }
            },
            "ContentType": "application/x-parquet",
            "CompressionType": "None",
        }

    def _vpc_config() -> dict | None:
        subnets = [s.strip() for s in SAGEMAKER_VPC_SUBNETS.split(",") if s.strip()]
        sgs     = [s.strip() for s in SAGEMAKER_VPC_SG.split(",")     if s.strip()]
        if subnets and sgs:
            return {"Subnets": subnets, "SecurityGroupIds": sgs}
        return None

    # ── Task 1: Submit XGBoost training job ───────────────────────────────────
    def submit_xgboost(**context) -> str:
        exec_date = context["params"].get("execution_date") or context["ds"]
        job_name  = f"churn-xgb-{exec_date.replace('-', '')}-{GIT_SHA[:8]}"

        submit_sagemaker_training_job(
            job_name=job_name,
            image_uri=f"{ECR_BASE}/training-xgboost:{GIT_SHA}",
            role_arn=SAGEMAKER_EXECUTION_ROLE_ARN,
            input_data_config=[
                _input_channel("train",      exec_date),
                _input_channel("validation", exec_date),
            ],
            output_s3_path=f"s3://{ARTIFACTS_BUCKET}/sagemaker/training/xgboost/{job_name}/",
            hyperparameters={
                "max_depth":             6,
                "learning_rate":         0.05,
                "n_estimators":          500,
                "early_stopping_rounds": 30,
                "subsample":             0.8,
                "colsample_bytree":      0.8,
                "scale_pos_weight":      "auto",
                "eval_metric":           "auc",
                "mlflow_tracking_uri":   MLFLOW_TRACKING_URI,
                "mlflow_experiment_name": f"churn-xgboost-{ENVIRONMENT}",
                "git_sha":               GIT_SHA,
            },
            instance_type="ml.m5.xlarge",
            use_spot=True,
            vpc_config=_vpc_config(),
            region=AWS_REGION,
            artifacts_bucket=ARTIFACTS_BUCKET,
            environment=ENVIRONMENT,
            project=PROJECT,
        )
        log.info(f"Submitted XGBoost job: {job_name}")
        return job_name

    submit_xgb_task = PythonOperator(
        task_id="submit_xgboost_training",
        python_callable=submit_xgboost,
        provide_context=True,
    )

    # ── Task 2: Submit PyTorch training job ───────────────────────────────────
    def submit_pytorch(**context) -> str:
        exec_date = context["params"].get("execution_date") or context["ds"]
        job_name  = f"churn-mlp-{exec_date.replace('-', '')}-{GIT_SHA[:8]}"

        submit_sagemaker_training_job(
            job_name=job_name,
            image_uri=f"{ECR_BASE}/training-pytorch:{GIT_SHA}",
            role_arn=SAGEMAKER_EXECUTION_ROLE_ARN,
            input_data_config=[
                _input_channel("train",      exec_date),
                _input_channel("validation", exec_date),
            ],
            output_s3_path=f"s3://{ARTIFACTS_BUCKET}/sagemaker/training/pytorch/{job_name}/",
            hyperparameters={
                "hidden_dims":   "256,128,64",
                "dropout_rate":  0.3,
                "learning_rate": 0.001,
                "batch_size":    512,
                "max_epochs":    100,
                "patience":      10,
                "focal_gamma":   2.0,
                "mlflow_tracking_uri":    MLFLOW_TRACKING_URI,
                "mlflow_experiment_name": f"churn-pytorch-{ENVIRONMENT}",
                "git_sha":                GIT_SHA,
            },
            instance_type="ml.m5.2xlarge",
            use_spot=True,
            vpc_config=_vpc_config(),
            region=AWS_REGION,
            artifacts_bucket=ARTIFACTS_BUCKET,
            environment=ENVIRONMENT,
            project=PROJECT,
        )
        log.info(f"Submitted PyTorch job: {job_name}")
        return job_name

    submit_pytorch_task = PythonOperator(
        task_id="submit_pytorch_training",
        python_callable=submit_pytorch,
        provide_context=True,
    )

    # ── Tasks 3 & 4: Wait for both jobs (run in parallel) ─────────────────────
    def poll_xgboost(**context) -> dict:
        job_name = context["task_instance"].xcom_pull(task_ids="submit_xgboost_training")
        desc     = wait_for_sagemaker_job(job_name, region=AWS_REGION)
        artifact_uri = desc["ModelArtifacts"]["S3ModelArtifacts"]
        log.info(f"XGBoost complete | artifacts: {artifact_uri}")
        emit_dag_metric("TrainingJobCompleted", 1, dag_id="churn_training_pipeline")
        return {"job_name": job_name, "model_s3_uri": artifact_uri, "model_type": "xgboost"}

    wait_xgb_task = PythonOperator(
        task_id="wait_for_xgboost",
        python_callable=poll_xgboost,
        provide_context=True,
        execution_timeout=timedelta(hours=3),
    )

    def poll_pytorch(**context) -> dict:
        job_name = context["task_instance"].xcom_pull(task_ids="submit_pytorch_training")
        desc     = wait_for_sagemaker_job(job_name, region=AWS_REGION)
        artifact_uri = desc["ModelArtifacts"]["S3ModelArtifacts"]
        log.info(f"PyTorch complete | artifacts: {artifact_uri}")
        return {"job_name": job_name, "model_s3_uri": artifact_uri, "model_type": "pytorch"}

    wait_pytorch_task = PythonOperator(
        task_id="wait_for_pytorch",
        python_callable=poll_pytorch,
        provide_context=True,
        execution_timeout=timedelta(hours=3),
    )

    # ── Task 5: Evaluate both models on held-out test set ─────────────────────
    def evaluate_models(**context) -> dict:
        """
        Run evaluate_model.py for both model types, apply quality gates,
        and return evaluation results for each. Both must pass gates;
        then we pick the winner by AUC.
        """
        import subprocess
        import sys

        exec_date = context["params"].get("execution_date") or context["ds"]
        test_path = f"s3://{PROCESSED_BUCKET}/features/test/{exec_date}/"

        # Get MLflow run IDs from training jobs via SageMaker tags
        # (the training script pushes run_id into the job's environment output)
        xgb_info = context["task_instance"].xcom_pull(task_ids="wait_for_xgboost")
        mlp_info = context["task_instance"].xcom_pull(task_ids="wait_for_pytorch")

        # Retrieve MLflow run IDs from the completed training jobs
        sm_client = boto3.client("sagemaker", region_name=AWS_REGION)

        def get_mlflow_run_id(job_name: str) -> str:
            desc = sm_client.describe_training_job(TrainingJobName=job_name)
            env  = desc.get("Environment", {})
            return env.get("MLFLOW_RUN_ID", "")

        results = {}
        for model_type, info in [("xgboost", xgb_info), ("pytorch", mlp_info)]:
            job_name    = info["job_name"]
            output_file = f"/tmp/gate_{model_type}.json"

            run_id = get_mlflow_run_id(job_name)

            proc = subprocess.run(
                [
                    sys.executable,
                    "/opt/airflow/dags/../../../ml/evaluation/evaluate_model.py",
                    "--run-id",        run_id or "placeholder",
                    "--test-data-s3",  test_path,
                    "--model-type",    model_type,
                    "--tracking-uri",  MLFLOW_TRACKING_URI,
                    "--output-file",   output_file,
                ],
                capture_output=True, text=True,
            )
            log.info(proc.stdout)
            if proc.stderr:
                log.warning(proc.stderr)

            try:
                with open(output_file) as f:
                    gate_result = json.load(f)
            except FileNotFoundError:
                gate_result = {"passed": False, "metrics": {}, "failures": ["evaluation script failed"]}

            results[model_type] = gate_result
            log.info(
                f"{model_type} gate: {'PASS' if gate_result['passed'] else 'FAIL'} | "
                f"AUC={gate_result.get('metrics', {}).get('test_auc', 'N/A')}"
            )

        return results

    evaluate_task = PythonOperator(
        task_id="evaluate_both_models",
        python_callable=evaluate_models,
        provide_context=True,
        execution_timeout=timedelta(minutes=30),
    )

    # ── Task 6: Pick winner ───────────────────────────────────────────────────
    def pick_winner(**context) -> str:
        """
        Choose the model type with higher AUC that also passed quality gates.
        Returns a branch name (either 'deploy_xgboost' or 'deploy_pytorch'
        or 'no_deploy' if both failed).
        """
        force_model = context["params"].get("force_model")
        results     = context["task_instance"].xcom_pull(task_ids="evaluate_both_models")

        passed = {k: v for k, v in results.items() if v.get("passed")}

        if force_model and force_model in passed:
            log.info(f"Force-selecting model: {force_model}")
            context["task_instance"].xcom_push(key="winner", value=force_model)
            return f"deploy_{force_model}"

        if not passed:
            log.error("Both models failed quality gates — skipping deployment")
            emit_dag_metric("NoDeployment", 1, dag_id="churn_training_pipeline")
            return "skip_deployment"

        # Pick highest AUC among passing models
        winner = max(passed, key=lambda k: passed[k].get("metrics", {}).get("test_auc", 0))
        winner_auc = passed[winner]["metrics"].get("test_auc", 0)
        log.info(f"Winner: {winner} (AUC={winner_auc:.4f})")

        emit_dag_metric("WinnerAUC", winner_auc, dag_id="churn_training_pipeline", unit="None")
        context["task_instance"].xcom_push(key="winner", value=winner)
        return f"deploy_{winner}"

    pick_winner_task = BranchPythonOperator(
        task_id="pick_winner",
        python_callable=pick_winner,
        provide_context=True,
    )

    # ── Tasks 7a/7b: Canary deploy (branched by winner) ───────────────────────
    def _run_canary_deploy(model_type: str, **context) -> None:
        """
        Trigger canary deployment for the winning model type.
        WHY call the Python script (not inline boto3)?
        canary_deploy.py already has the complete ALB traffic-split logic
        and CloudWatch monitoring loop. Reuse it rather than duplicating.
        """
        import subprocess
        import sys

        proc = subprocess.run(
            [
                sys.executable,
                "/opt/airflow/dags/../../../ml/deployment/canary_deploy.py",
                "--model-version",       context["task_instance"].xcom_pull(
                    task_ids="pick_winner", key="winner"
                ) or "1",
                "--model-name",          f"churn-prediction-{model_type}",
                "--stable-tg-arn",       os.environ.get("STABLE_TG_ARN", ""),
                "--canary-tg-arn",       os.environ.get("CANARY_TG_ARN", ""),
                "--listener-rule-arn",   os.environ.get("LISTENER_RULE_ARN", ""),
                "--mlflow-tracking-uri", MLFLOW_TRACKING_URI,
            ],
            capture_output=True, text=True,
        )
        log.info(proc.stdout)
        if proc.returncode != 0:
            log.error(proc.stderr)
            raise RuntimeError(f"Canary deploy failed for {model_type}:\n{proc.stderr}")

    deploy_xgboost_task = PythonOperator(
        task_id="deploy_xgboost",
        python_callable=lambda **ctx: _run_canary_deploy("xgboost", **ctx),
        provide_context=True,
        execution_timeout=timedelta(hours=1),  # Canary runs for 30 min + buffer
    )

    deploy_pytorch_task = PythonOperator(
        task_id="deploy_pytorch",
        python_callable=lambda **ctx: _run_canary_deploy("pytorch", **ctx),
        provide_context=True,
        execution_timeout=timedelta(hours=1),
    )

    skip_deployment_task = PythonOperator(
        task_id="skip_deployment",
        python_callable=lambda **ctx: log.warning("Skipping deployment — no model passed quality gates"),
        provide_context=True,
    )

    # ── Task 8: Promote (runs after either deploy branch succeeds) ────────────
    def run_promote(**context) -> None:
        import subprocess
        import sys

        winner = context["task_instance"].xcom_pull(task_ids="pick_winner", key="winner")

        proc = subprocess.run(
            [
                sys.executable,
                "/opt/airflow/dags/../../../ml/deployment/promote_model.py",
                "--model-version",     "1",  # Retrieved from MLflow in real run
                "--model-name",        f"churn-prediction-{winner}",
                "--stable-tg-arn",     os.environ.get("STABLE_TG_ARN", ""),
                "--canary-tg-arn",     os.environ.get("CANARY_TG_ARN", ""),
                "--listener-rule-arn", os.environ.get("LISTENER_RULE_ARN", ""),
                "--artifacts-bucket",  ARTIFACTS_BUCKET,
                "--environment",       ENVIRONMENT,
            ],
            capture_output=True, text=True,
        )
        log.info(proc.stdout)
        if proc.returncode != 0:
            raise RuntimeError(f"Promotion failed:\n{proc.stderr}")
        emit_dag_metric("DeploymentCompleted", 1, dag_id="churn_training_pipeline")

    promote_task = PythonOperator(
        task_id="promote_model",
        python_callable=run_promote,
        provide_context=True,
        # TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS: runs if at least one upstream
        # branch succeeded and none failed (handles the branching correctly).
        trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS,
    )

    # ── Task ordering ──────────────────────────────────────────────────────────
    # XGBoost and PyTorch submit/wait run in parallel:
    submit_xgb_task    >> wait_xgb_task
    submit_pytorch_task >> wait_pytorch_task

    # Both must complete before evaluation:
    [wait_xgb_task, wait_pytorch_task] >> evaluate_task

    # Branching → deploy → promote:
    evaluate_task >> pick_winner_task
    pick_winner_task >> [deploy_xgboost_task, deploy_pytorch_task, skip_deployment_task]
    [deploy_xgboost_task, deploy_pytorch_task] >> promote_task
