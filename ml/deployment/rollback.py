"""
Model rollback — immediately restores the previous production model.

WHY a dedicated rollback script (not just re-running deploy)?
  - Speed: rollback must be instant (seconds, not minutes)
  - Safety: it must not require the training pipeline to re-run
  - Clarity: the rollback record should reference the incident that triggered it

Triggered by:
  1. Canary monitoring auto-rollback (called from canary_deploy.py)
  2. Manual trigger via GitHub Actions workflow_dispatch
  3. CloudWatch alarm → SNS → Lambda → this script (Phase 8)

Rollback mechanism:
  1. Read latest.json from S3 to get the PREVIOUS deployment record
  2. Shift 100% traffic back to the stable target group
  3. Transition the problematic model version to 'Archived' in MLflow
  4. Transition the previous Production version back to 'Production'
  5. Write incident record to S3

No Kubernetes changes needed: the stable target group still points to
the previous model's pods (they were never terminated during canary deploy).
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger(__name__)

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-bucket",   required=True)
    parser.add_argument("--model-name",         default="churn-prediction-xgboost")
    parser.add_argument("--failed-version",     required=True, help="Model version being rolled back")
    parser.add_argument("--stable-tg-arn",      required=True)
    parser.add_argument("--canary-tg-arn",       required=True)
    parser.add_argument("--listener-rule-arn",  required=True)
    parser.add_argument("--reason",             required=True, help="Rollback reason (for audit trail)")
    parser.add_argument("--mlflow-tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI"))
    parser.add_argument("--environment",        default=os.environ.get("ENVIRONMENT", "dev"))
    return parser.parse_args()


def restore_traffic(args):
    """Route 100% traffic back to the stable (previous) target group immediately."""
    elbv2 = boto3.client("elbv2", region_name=REGION)
    log.info("Restoring 100% traffic to stable model...")

    elbv2.modify_rule(
        RuleArn=args.listener_rule_arn,
        Actions=[{
            "Type": "forward",
            "ForwardConfig": {
                "TargetGroups": [
                    {"TargetGroupArn": args.stable_tg_arn, "Weight": 100},
                    {"TargetGroupArn": args.canary_tg_arn,  "Weight": 0},
                ],
            },
        }],
    )
    log.info("Traffic restored to stable model (100%)")


def rollback_mlflow_registry(args, s3_client):
    """
    Archive the failed version and restore the previous Production version.
    Reads the pre-rollback deployment history to find what 'previous production' was.
    """
    import mlflow
    mlflow.set_tracking_uri(args.mlflow_tracking_uri)
    client = mlflow.tracking.MlflowClient()

    # Archive the failed version
    client.transition_model_version_stage(
        name=args.model_name,
        version=args.failed_version,
        stage="Archived",
        archive_existing_versions=False,
    )
    log.info(f"Archived failed model version {args.failed_version}")

    # Find the previous production version from deployment history
    response = s3_client.list_objects_v2(
        Bucket=args.artifacts_bucket,
        Prefix=f"deployments/{args.environment}/{args.model_name}/",
    )

    deployment_records = []
    for obj in response.get("Contents", []):
        if obj["Key"].endswith("latest.json"):
            continue
        try:
            body = s3_client.get_object(
                Bucket=args.artifacts_bucket,
                Key=obj["Key"],
            )["Body"].read()
            record = json.loads(body)
            deployment_records.append(record)
        except Exception:
            continue

    # Sort by deployed_at to find the second-most-recent (the one before the failed deploy)
    deployment_records.sort(key=lambda r: r.get("deployed_at", ""), reverse=True)
    previous_record = None
    for record in deployment_records:
        if record.get("model_version") != args.failed_version:
            previous_record = record
            break

    if previous_record:
        prev_version = previous_record["model_version"]
        log.info(f"Restoring previous production version: {prev_version}")
        client.transition_model_version_stage(
            name=args.model_name,
            version=prev_version,
            stage="Production",
            archive_existing_versions=False,
        )
    else:
        log.warning("Could not determine previous production version from deployment history")


def write_incident_record(args, s3_client):
    """
    Write a rollback/incident record to S3.
    Used for post-incident review and for tracking MTTR (Mean Time To Recovery).
    """
    incident = {
        "type":             "rollback",
        "model_name":       args.model_name,
        "failed_version":   args.failed_version,
        "reason":           args.reason,
        "environment":      args.environment,
        "rolled_back_at":   datetime.now(timezone.utc).isoformat(),
        "rolled_back_by":   os.environ.get("GITHUB_ACTOR", "automated"),
        "pipeline_run_id":  os.environ.get("GITHUB_RUN_ID", "unknown"),
        "git_sha":          os.environ.get("GIT_SHA", "unknown"),
    }

    key = (
        f"incidents/{args.environment}/{args.model_name}/"
        f"{datetime.now(timezone.utc).strftime('%Y/%m/%d/%H%M%S')}_rollback.json"
    )

    s3_client.put_object(
        Bucket=args.artifacts_bucket,
        Key=key,
        Body=json.dumps(incident, indent=2),
        ContentType="application/json",
    )
    log.info(f"Incident record written: s3://{args.artifacts_bucket}/{key}")
    return key


def main():
    args      = parse_args()
    s3_client = boto3.client("s3", region_name=REGION)

    log.warning(
        f"ROLLBACK INITIATED | Model: {args.model_name} | "
        f"Version: {args.failed_version} | Reason: {args.reason}"
    )

    # Step 1: Restore traffic (fastest — do this first to stop bleeding)
    restore_traffic(args)

    # Step 2: Update MLflow registry
    try:
        rollback_mlflow_registry(args, s3_client)
    except Exception as e:
        log.error(f"Failed to update MLflow registry during rollback: {e}")
        # Don't exit — traffic is already restored. Log and continue.

    # Step 3: Write incident record
    incident_key = write_incident_record(args, s3_client)

    log.warning(
        f"Rollback complete | Incident record: s3://{args.artifacts_bucket}/{incident_key}"
    )

    print(f"::set-output name=rollback_status::completed")
    print(f"::set-output name=incident_key::{incident_key}")
    sys.exit(0)


if __name__ == "__main__":
    main()
