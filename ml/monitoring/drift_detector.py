"""
Model Monitor Drift Detector

Reads the latest SageMaker Model Monitor violation report and decides whether
drift is severe enough to trigger a retraining run.

Two types of monitoring:
  1. Data Quality Monitor — detects feature distribution shift
       "The distribution of customer_tenure in live traffic no longer matches
        what the model was trained on."
       Triggered by: baseline constraints violations (null rates, value ranges,
       distribution distances like KS-test p-value < 0.05)

  2. Model Quality Monitor — detects concept drift (label/prediction accuracy)
       "The model's actual precision on labeled ground-truth has dropped from
        0.82 to 0.71 since last month."
       Triggered by: ground truth labels arriving in S3 (from CRM churn events)
       and being compared to the model's predictions.

WHY separate detector (not just AlertManager)?
  AlertManager fires on metrics — it can alert when drift is detected, but
  it cannot trigger a SageMaker/Airflow retraining run. This script:
    1. Reads the violation report from S3
    2. Scores the severity (# violations, which features)
    3. Decides: alert only, or alert + trigger retraining
    4. Publishes a CloudWatch metric (DriftScore) for Grafana
    5. Optionally POSTs to the Airflow REST API to trigger training_pipeline

Called from: a scheduled Lambda (every 6h) or an Airflow data_quality task.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

AWS_REGION       = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT      = os.environ.get("ENVIRONMENT", "dev")
PROJECT          = os.environ.get("PROJECT", "churn-platform")
ARTIFACTS_BUCKET = os.environ["ARTIFACTS_BUCKET"]
AIRFLOW_API_URL  = os.environ.get("AIRFLOW_API_URL", "")      # e.g. http://airflow-webserver.airflow.svc:8080
AIRFLOW_API_SECRET = os.environ.get("AIRFLOW_API_SECRET_NAME", "")

# Thresholds for retraining decision
RETRAINING_VIOLATION_THRESHOLD = int(os.environ.get("RETRAINING_VIOLATION_THRESHOLD", "5"))
RETRAINING_DRIFT_SCORE_THRESHOLD = float(os.environ.get("RETRAINING_DRIFT_SCORE_THRESHOLD", "0.3"))


@dataclass
class ViolationReport:
    """Parsed output of a Model Monitor violation report."""
    report_s3_uri:        str
    monitoring_type:      str    # "data_quality" or "model_quality"
    violations:           list[dict] = field(default_factory=list)
    violation_count:      int = 0
    critical_features:    list[str] = field(default_factory=list)
    drift_score:          float = 0.0    # 0.0 (no drift) to 1.0 (severe drift)
    trigger_retraining:   bool = False
    report_timestamp:     str = ""


def _fetch_violation_report(s3_uri: str) -> dict:
    """Download and parse the Model Monitor violation JSON report."""
    s3     = boto3.client("s3", region_name=AWS_REGION)
    bucket = s3_uri.split("/")[2]
    key    = "/".join(s3_uri.split("/")[3:])

    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)
    except s3.exceptions.NoSuchKey:
        log.warning(f"No violation report at {s3_uri} — monitoring job may not have run yet")
        return {}
    except Exception as e:
        log.error(f"Failed to fetch violation report: {e}")
        return {}


def _get_latest_report_uri(monitoring_schedule_name: str) -> str | None:
    """
    Find the S3 URI of the most recent monitoring execution report.
    SageMaker stores reports at:
      s3://{bucket}/model-monitor/reports/{schedule-name}/{execution-id}/violations.json
    """
    sm    = boto3.client("sagemaker", region_name=AWS_REGION)
    try:
        executions = sm.list_monitoring_executions(
            MonitoringScheduleName=monitoring_schedule_name,
            SortBy="CreationTime",
            SortOrder="Descending",
            MaxResults=1,
        )
        items = executions.get("MonitoringExecutionSummaries", [])
        if not items:
            log.warning(f"No monitoring executions found for {monitoring_schedule_name}")
            return None

        latest = items[0]
        status = latest.get("MonitoringExecutionStatus", "")
        log.info(f"Latest execution: {latest['MonitoringExecutionArn']} status={status}")

        if status not in ("Completed", "CompletedWithViolations"):
            log.info(f"Latest execution not yet complete (status={status})")
            return None

        output_uri = latest.get("ProcessingJobArn", "")
        # Get the actual report URI from the processing job outputs
        pj_name = latest["ProcessingJobArn"].split("/")[-1]
        pj      = sm.describe_processing_job(ProcessingJobName=pj_name)
        for output in pj.get("ProcessingOutputConfig", {}).get("Outputs", []):
            if output.get("OutputName") == "monitoring_output":
                return output["S3Output"]["S3Uri"] + "/violations.json"

        return None
    except Exception as e:
        log.error(f"Failed to get latest monitoring execution: {e}")
        return None


def _compute_drift_score(violations: list[dict]) -> tuple[float, list[str]]:
    """
    Convert the list of violations into a 0–1 drift score.

    Scoring:
      - Each violation adds to the score
      - High-importance features (tenure, monthly_charges, support_tickets)
        are weighted 3x — drift in these features matters more for churn prediction
      - Max score is capped at 1.0

    Returns: (drift_score, critical_feature_names)
    """
    HIGH_IMPORTANCE_FEATURES = {
        "customer_tenure_months", "monthly_charges", "support_tickets_90d",
        "contract_type", "payment_method", "total_charges",
    }

    score            = 0.0
    critical_features = []

    for v in violations:
        feature  = v.get("feature_name", "")
        vtype    = v.get("constraint_check_type", "")
        severity = 1.0

        # Weight high-importance features more heavily
        if feature in HIGH_IMPORTANCE_FEATURES:
            severity = 3.0
            critical_features.append(feature)

        # Categorical distribution violations are more severe than null rate violations
        # (null rate drift = upstream data issue; distribution drift = concept drift)
        if "distribution" in vtype.lower() or "baseline" in vtype.lower():
            severity *= 1.5

        score += severity * 0.05    # Each violation contributes 5% (weighted)

    return min(score, 1.0), list(set(critical_features))


def _trigger_retraining(reason: str, drift_score: float, critical_features: list[str]) -> bool:
    """
    POST to Airflow REST API to trigger the training_pipeline DAG.

    WHY trigger via Airflow API (not directly calling SageMaker)?
      The full training pipeline includes:
        1. Feature re-export (ensure training uses fresh features)
        2. Data validation gate (refuse to train on corrupt data)
        3. Parallel XGBoost + PyTorch training
        4. Model evaluation quality gate
        5. Canary deployment
      Triggering SageMaker directly would skip steps 1, 2, 4, 5.
      The Airflow DAG already encodes all these steps correctly.
    """
    if not AIRFLOW_API_URL:
        log.warning("AIRFLOW_API_URL not set — cannot trigger retraining automatically")
        return False

    import urllib.request

    # Get Airflow API credentials from Secrets Manager
    sm   = boto3.client("secretsmanager", region_name=AWS_REGION)
    creds_raw = sm.get_secret_value(SecretId=AIRFLOW_API_SECRET)["SecretString"]
    creds     = json.loads(creds_raw)
    username  = creds["username"]
    password  = creds["password"]

    import base64
    token    = base64.b64encode(f"{username}:{password}".encode()).decode()

    payload = json.dumps({
        "conf": {
            "trigger_reason":      "model_monitor_drift",
            "drift_score":         drift_score,
            "critical_features":   critical_features,
            "triggered_at":        datetime.now(timezone.utc).isoformat(),
            "triggered_by":        "drift_detector",
        }
    }).encode()

    url = f"{AIRFLOW_API_URL}/api/v1/dags/churn_training_pipeline/dagRuns"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Basic {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body   = json.loads(resp.read().decode())
            run_id = body.get("dag_run_id", "unknown")
            log.info(f"Retraining triggered successfully: dag_run_id={run_id}")
            return True
    except Exception as e:
        log.error(f"Failed to trigger retraining via Airflow API: {e}")
        return False


def _emit_drift_metrics(
    drift_score: float,
    violation_count: int,
    monitoring_type: str,
    retraining_triggered: bool,
) -> None:
    """Emit drift metrics to CloudWatch for Grafana dashboards and alerting."""
    cw = boto3.client("cloudwatch", region_name=AWS_REGION)

    dimensions = [
        {"Name": "Environment",     "Value": ENVIRONMENT},
        {"Name": "MonitoringType",  "Value": monitoring_type},
    ]

    cw.put_metric_data(
        Namespace=f"{PROJECT}/ModelMonitor",
        MetricData=[
            {
                "MetricName": "DriftScore",
                "Value":      drift_score,
                "Unit":       "None",
                "Dimensions": dimensions,
                "Timestamp":  datetime.now(timezone.utc),
            },
            {
                "MetricName": "ViolationCount",
                "Value":      float(violation_count),
                "Unit":       "Count",
                "Dimensions": dimensions,
                "Timestamp":  datetime.now(timezone.utc),
            },
            {
                "MetricName": "RetrainingTriggered",
                "Value":      1.0 if retraining_triggered else 0.0,
                "Unit":       "Count",
                "Dimensions": dimensions,
                "Timestamp":  datetime.now(timezone.utc),
            },
        ],
    )
    log.info(f"CloudWatch metrics emitted: DriftScore={drift_score:.3f}, Violations={violation_count}")


def analyse_schedule(monitoring_schedule_name: str, monitoring_type: str) -> ViolationReport:
    """
    Full analysis workflow for one monitoring schedule:
      1. Find the latest execution report URI
      2. Fetch and parse the violations JSON
      3. Compute drift score
      4. Decide whether to trigger retraining
      5. Emit CloudWatch metrics
    """
    report = ViolationReport(
        report_s3_uri    = "",
        monitoring_type  = monitoring_type,
        report_timestamp = datetime.now(timezone.utc).isoformat(),
    )

    report_uri = _get_latest_report_uri(monitoring_schedule_name)
    if not report_uri:
        log.info(f"No completed execution for {monitoring_schedule_name} — skipping")
        _emit_drift_metrics(0.0, 0, monitoring_type, False)
        return report

    report.report_s3_uri = report_uri
    raw_report           = _fetch_violation_report(report_uri)
    violations           = raw_report.get("violations", [])

    report.violations      = violations
    report.violation_count = len(violations)

    drift_score, critical_features = _compute_drift_score(violations)
    report.drift_score         = drift_score
    report.critical_features   = critical_features

    log.info(
        f"Schedule={monitoring_schedule_name} | "
        f"Violations={report.violation_count} | "
        f"DriftScore={drift_score:.3f} | "
        f"CriticalFeatures={critical_features}"
    )

    # Retraining decision: ANY critical feature violated, OR drift score above threshold,
    # OR too many total violations
    should_retrain = (
        len(critical_features) > 0
        or drift_score >= RETRAINING_DRIFT_SCORE_THRESHOLD
        or report.violation_count >= RETRAINING_VIOLATION_THRESHOLD
    )

    retraining_triggered = False
    if should_retrain:
        reason = (
            f"Drift detected: score={drift_score:.3f}, "
            f"violations={report.violation_count}, "
            f"critical_features={critical_features}"
        )
        log.warning(f"Retraining condition met: {reason}")
        retraining_triggered = _trigger_retraining(reason, drift_score, critical_features)

    report.trigger_retraining = retraining_triggered
    _emit_drift_metrics(drift_score, report.violation_count, monitoring_type, retraining_triggered)

    # Write report summary to S3 for audit trail
    summary = {
        "timestamp":              report.report_timestamp,
        "schedule":               monitoring_schedule_name,
        "monitoring_type":        monitoring_type,
        "violation_count":        report.violation_count,
        "drift_score":            drift_score,
        "critical_features":      critical_features,
        "retraining_triggered":   retraining_triggered,
        "source_report_uri":      report_uri,
    }
    s3_key = (
        f"model-monitor/drift-summaries/{ENVIRONMENT}/"
        f"{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/"
        f"{monitoring_schedule_name}-{int(time.time())}.json"
    )
    boto3.client("s3", region_name=AWS_REGION).put_object(
        Bucket=ARTIFACTS_BUCKET,
        Key=s3_key,
        Body=json.dumps(summary, indent=2).encode(),
        ContentType="application/json",
    )
    log.info(f"Drift summary written to s3://{ARTIFACTS_BUCKET}/{s3_key}")

    return report


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-quality-schedule",  required=True)
    parser.add_argument("--model-quality-schedule",  required=True)
    args = parser.parse_args()

    dq_report = analyse_schedule(args.data_quality_schedule,  "data_quality")
    mq_report = analyse_schedule(args.model_quality_schedule, "model_quality")

    any_critical = (
        dq_report.violation_count > 0
        or mq_report.violation_count > 0
    )

    result = {
        "data_quality":  {
            "violations":   dq_report.violation_count,
            "drift_score":  dq_report.drift_score,
            "retraining":   dq_report.trigger_retraining,
        },
        "model_quality": {
            "violations":   mq_report.violation_count,
            "drift_score":  mq_report.drift_score,
            "retraining":   mq_report.trigger_retraining,
        },
        "any_critical": any_critical,
    }
    Path("/tmp/drift_result.json").write_text(json.dumps(result, indent=2))
    log.info(json.dumps(result, indent=2))

    if any_critical:
        raise SystemExit(1)   # Non-zero exit → Airflow marks task failed → Slack alert


if __name__ == "__main__":
    main()
