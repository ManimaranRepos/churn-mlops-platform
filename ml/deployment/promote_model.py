"""
Promote canary model to 100% production traffic.

Called after canary_deploy.py exits 0 (canary healthy).

Steps:
  1. Route 100% traffic to the (formerly canary) target group
  2. Transition MLflow model version from Staging → Production
  3. Archive the previous Production version in MLflow
  4. Write deployment record to S3 (audit trail)

This script does NOT modify Kubernetes deployments directly.
The ArgoCD pipeline (Phase 3) already updated the image tag in Git,
and ArgoCD synced the new deployment. This script only manages the
ALB traffic weight and MLflow model registry state.
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
    parser.add_argument("--model-version",     required=True)
    parser.add_argument("--model-name",        default="churn-prediction-xgboost")
    parser.add_argument("--stable-tg-arn",     required=True)
    parser.add_argument("--canary-tg-arn",      required=True)
    parser.add_argument("--listener-rule-arn", required=True)
    parser.add_argument("--artifacts-bucket",  required=True)
    parser.add_argument("--mlflow-tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI"))
    parser.add_argument("--environment",       default=os.environ.get("ENVIRONMENT", "dev"))
    return parser.parse_args()


def promote_traffic(args):
    """Shift 100% of traffic to the canary target group (which is now the new stable)."""
    elbv2 = boto3.client("elbv2", region_name=REGION)
    log.info("Promoting canary to 100% production traffic...")

    elbv2.modify_rule(
        RuleArn=args.listener_rule_arn,
        Actions=[{
            "Type": "forward",
            "ForwardConfig": {
                "TargetGroups": [
                    # NOTE: formerly 'canary' becomes the new 'stable' at 100%
                    # The stale 'stable' target group is now at 0% and can be cleaned up
                    {"TargetGroupArn": args.canary_tg_arn,  "Weight": 100},
                    {"TargetGroupArn": args.stable_tg_arn,  "Weight": 0},
                ],
            },
        }],
    )
    log.info("Traffic promotion complete: 100% on new model")


def promote_mlflow_model(args):
    """
    Transition model from Staging → Production in MLflow Model Registry.
    archive_existing_versions=True ensures the previous Production version
    is moved to Archived so we have a clear audit trail of what was ever in prod.
    """
    import mlflow
    mlflow.set_tracking_uri(args.mlflow_tracking_uri)
    client = mlflow.tracking.MlflowClient()

    client.transition_model_version_stage(
        name=args.model_name,
        version=args.model_version,
        stage="Production",
        archive_existing_versions=True,  # Archive previous Production version
    )

    log.info(
        f"MLflow model '{args.model_name}' v{args.model_version} → Production "
        f"(previous version archived)"
    )


def write_deployment_record(args, s3_client):
    """
    Write a deployment record to S3 for audit trail.
    WHY: CloudTrail records API calls but not business context.
    This record links the deployment to a model version, Git SHA, and timestamp.
    It's also used by the rollback script to know what to roll back to.
    """
    record = {
        "model_name":       args.model_name,
        "model_version":    args.model_version,
        "environment":      args.environment,
        "deployed_at":      datetime.now(timezone.utc).isoformat(),
        "deployed_by":      os.environ.get("GITHUB_ACTOR", "unknown"),
        "git_sha":          os.environ.get("GIT_SHA", "unknown"),
        "pipeline_run_id":  os.environ.get("GITHUB_RUN_ID", "unknown"),
        "canary_tg_arn":    args.canary_tg_arn,
        "stable_tg_arn":    args.stable_tg_arn,
        "listener_rule_arn": args.listener_rule_arn,
    }

    key = (
        f"deployments/{args.environment}/{args.model_name}/"
        f"{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/"
        f"{args.model_version}.json"
    )

    s3_client.put_object(
        Bucket=args.artifacts_bucket,
        Key=key,
        Body=json.dumps(record, indent=2),
        ContentType="application/json",
    )
    log.info(f"Deployment record written: s3://{args.artifacts_bucket}/{key}")

    # Also write a 'latest' pointer so rollback.py can find it easily
    latest_key = f"deployments/{args.environment}/{args.model_name}/latest.json"
    s3_client.put_object(
        Bucket=args.artifacts_bucket,
        Key=latest_key,
        Body=json.dumps(record, indent=2),
        ContentType="application/json",
    )


def main():
    args      = parse_args()
    s3_client = boto3.client("s3", region_name=REGION)

    promote_traffic(args)
    promote_mlflow_model(args)
    write_deployment_record(args, s3_client)

    log.info(
        f"Promotion complete | "
        f"Model: {args.model_name} v{args.model_version} | "
        f"Environment: {args.environment}"
    )

    print(f"::set-output name=deployment_status::promoted")
    print(f"::set-output name=model_version::{args.model_version}")
    sys.exit(0)


if __name__ == "__main__":
    main()
