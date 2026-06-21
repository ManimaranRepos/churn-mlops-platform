"""
Drift Detector Lambda — EventBridge entry point

Invoked by EventBridge when a SageMaker Model Monitor execution completes.
Delegates to drift_detector.py logic via direct import.

Event payload from EventBridge:
  {
    "source": "aws.sagemaker",
    "detail-type": "SageMaker Model Monitor Monitoring Execution Status Change",
    "detail": {
      "MonitoringScheduleName": "churn-platform-dev-data-quality-schedule",
      "MonitoringExecutionStatus": "CompletedWithViolations",
      "ProcessingJobArn": "arn:aws:sagemaker:...",
      "ScheduledTime": "2024-01-01T06:00:00Z"
    }
  }
"""

from __future__ import annotations

import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

# Set env vars before importing drift_detector (it reads them at module level)
os.environ.setdefault("ENVIRONMENT", os.environ.get("ENVIRONMENT", "dev"))
os.environ.setdefault("PROJECT",     os.environ.get("PROJECT", "churn-platform"))

# Import the actual detection logic
import sys
sys.path.insert(0, "/var/task")

DATA_QUALITY_SCHEDULE = os.environ["DATA_QUALITY_SCHEDULE"]
MODEL_QUALITY_SCHEDULE = os.environ["MODEL_QUALITY_SCHEDULE"]
SNS_TOPIC_ARN_WARNING  = os.environ.get("SNS_TOPIC_ARN_WARNING", "")
AWS_REGION             = os.environ.get("AWS_REGION", "us-east-1")


def _notify_sns(subject: str, message: str) -> None:
    if not SNS_TOPIC_ARN_WARNING:
        return
    try:
        boto3.client("sns", region_name=AWS_REGION).publish(
            TopicArn=SNS_TOPIC_ARN_WARNING,
            Subject=subject,
            Message=message,
        )
    except Exception as e:
        log.error(f"SNS publish failed: {e}")


def handler(event: dict, context) -> dict:
    """
    Lambda handler. Identifies which schedule fired, runs drift analysis for that
    monitoring type, and optionally triggers retraining.
    """
    log.info(f"Event: {json.dumps(event)}")

    detail       = event.get("detail", {})
    schedule     = detail.get("MonitoringScheduleName", "")
    exec_status  = detail.get("MonitoringExecutionStatus", "")

    log.info(f"Processing monitoring execution: schedule={schedule}, status={exec_status}")

    if exec_status not in ("Completed", "CompletedWithViolations"):
        log.info(f"Execution status {exec_status} — nothing to do")
        return {"statusCode": 200, "message": "No violations to check"}

    # Import here to avoid cold-start cost when status check exits early
    from drift_detector import analyse_schedule

    # Determine monitoring type from schedule name
    if schedule == DATA_QUALITY_SCHEDULE:
        monitoring_type = "data_quality"
    elif schedule == MODEL_QUALITY_SCHEDULE:
        monitoring_type = "model_quality"
    else:
        log.warning(f"Unknown schedule: {schedule} — analysing as data_quality")
        monitoring_type = "data_quality"

    try:
        report = analyse_schedule(schedule, monitoring_type)

        result = {
            "schedule":           schedule,
            "monitoring_type":    monitoring_type,
            "violation_count":    report.violation_count,
            "drift_score":        report.drift_score,
            "critical_features":  report.critical_features,
            "retraining_triggered": report.trigger_retraining,
        }
        log.info(f"Analysis complete: {json.dumps(result)}")

        if exec_status == "CompletedWithViolations" and report.violation_count > 0:
            _notify_sns(
                subject=f"[{os.environ.get('ENVIRONMENT', 'dev').upper()}] Model Monitor: {report.violation_count} violations",
                message=json.dumps(result, indent=2),
            )

        return {"statusCode": 200, **result}

    except Exception as e:
        log.error(f"Drift analysis failed: {e}", exc_info=True)
        _notify_sns(
            subject=f"Drift Detector Lambda Error: {schedule}",
            message=str(e),
        )
        return {"statusCode": 500, "error": str(e)}
