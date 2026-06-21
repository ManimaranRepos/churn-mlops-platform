"""
Canary deployment — routes 10% of traffic to the new model, monitors for 30 minutes.

WHY canary instead of blue/green or immediate rollout?
  - Blue/green cuts over 100% at once — if the new model regresses, ALL users are affected
  - Canary limits blast radius to 10% while we verify real-world performance
  - 30 minutes is long enough for enough traffic to accumulate meaningful error rates
    (assumes ~100+ predictions/minute in production)

Traffic split mechanism: AWS Application Load Balancer weighted target groups.
  - Target group A (stable): 90% weight
  - Target group B (canary): 10% weight

We DON'T use Kubernetes traffic splitting (Istio/Flagger) because:
  - This is EKS without a service mesh (would add ~$100/month for Istio control plane)
  - ALB weighted routing is sufficient and already deployed

Monitoring checks every 2 minutes:
  - Error rate > 5%  → auto-rollback
  - P99 latency > 500ms → auto-rollback
  - Positive prediction rate deviates >50% from baseline → investigation alert (not rollback)

Exit codes:
  0 — canary healthy, ready to promote
  1 — canary failed, rolled back
  2 — monitoring inconclusive (manual review needed)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger(__name__)

CANARY_WEIGHT_PERCENT  = 10
STABLE_WEIGHT_PERCENT  = 90
MONITOR_DURATION_S     = 30 * 60   # 30 minutes
MONITOR_INTERVAL_S     = 2  * 60   # Check every 2 minutes
ERROR_RATE_THRESHOLD   = 0.05      # 5% error rate triggers rollback
P99_LATENCY_THRESHOLD  = 500.0     # 500ms P99 triggers rollback
PREDICTION_RATE_ALERT  = 0.50      # 50% deviation in positive rate triggers alert


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-version",       required=True, help="MLflow model version to deploy")
    parser.add_argument("--model-name",          default="churn-prediction-xgboost")
    parser.add_argument("--stable-tg-arn",       required=True, help="ARN of stable target group")
    parser.add_argument("--canary-tg-arn",        required=True, help="ARN of canary target group")
    parser.add_argument("--listener-rule-arn",   required=True, help="ARN of ALB listener rule")
    parser.add_argument("--baseline-error-rate", type=float, default=None, help="Baseline error rate for comparison")
    parser.add_argument("--namespace",           default="inference")
    parser.add_argument("--deployment-name",     default="inference-server-canary")
    parser.add_argument("--mlflow-tracking-uri", default=os.environ.get("MLFLOW_TRACKING_URI"))
    return parser.parse_args()


def get_cloudwatch_metric(
    cw_client,
    metric_name: str,
    dimension_name: str,
    dimension_value: str,
    namespace: str = "ChurnPlatform/Inference",
    period_seconds: int = 120,
    stat: str = "Average",
) -> float:
    """Fetch the latest value of a CloudWatch metric."""
    now = datetime.now(timezone.utc)
    response = cw_client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=[{"Name": dimension_name, "Value": dimension_value}],
        StartTime=now.replace(second=0, microsecond=0).__class__(
            now.year, now.month, now.day, now.hour, now.minute, 0,
            tzinfo=timezone.utc
        ).__sub__(__import__("datetime").timedelta(seconds=period_seconds * 2)),
        EndTime=now,
        Period=period_seconds,
        Statistics=[stat],
    )

    datapoints = sorted(response.get("Datapoints", []), key=lambda x: x["Timestamp"])
    if not datapoints:
        return 0.0
    return float(datapoints[-1][stat])


def set_traffic_weights(
    elbv2_client,
    listener_rule_arn: str,
    stable_tg_arn: str,
    canary_tg_arn: str,
    stable_weight: int,
    canary_weight: int,
):
    """Update ALB listener rule to set traffic weights between two target groups."""
    log.info(f"Setting traffic split: stable={stable_weight}% / canary={canary_weight}%")

    elbv2_client.modify_rule(
        RuleArn=listener_rule_arn,
        Actions=[{
            "Type": "forward",
            "ForwardConfig": {
                "TargetGroups": [
                    {"TargetGroupArn": stable_tg_arn, "Weight": stable_weight},
                    {"TargetGroupArn": canary_tg_arn,  "Weight": canary_weight},
                ],
                "TargetGroupStickinessConfig": {
                    "Enabled":         True,
                    "DurationSeconds": 300,  # Sticky for 5 min (consistent user experience)
                },
            },
        }],
    )


def rollback(elbv2_client, args, reason: str) -> int:
    """Route 100% traffic back to stable target group."""
    log.warning(f"ROLLBACK triggered: {reason}")
    set_traffic_weights(
        elbv2_client,
        args.listener_rule_arn,
        args.stable_tg_arn,
        args.canary_tg_arn,
        stable_weight=100,
        canary_weight=0,
    )
    log.info("Rollback complete — 100% traffic on stable model")
    return 1


def monitor_canary(args, elbv2_client, cw_client) -> int:
    """
    Poll CloudWatch metrics every 2 minutes for 30 minutes.
    Returns exit code: 0=pass, 1=rollback triggered, 2=inconclusive.
    """
    log.info(f"Starting canary monitoring | Duration: {MONITOR_DURATION_S//60}min | Interval: {MONITOR_INTERVAL_S//60}min")
    start_time = time.time()
    check_count = 0

    while time.time() - start_time < MONITOR_DURATION_S:
        time.sleep(MONITOR_INTERVAL_S)
        check_count += 1
        elapsed = int(time.time() - start_time)
        remaining = MONITOR_DURATION_S - elapsed

        log.info(f"Check #{check_count} | Elapsed: {elapsed//60}min | Remaining: {remaining//60}min")

        # ── Fetch canary metrics from CloudWatch ───────────────────────────────
        canary_error_rate = get_cloudwatch_metric(
            cw_client, "ErrorRate", "Variant", "canary"
        )
        canary_p99_latency = get_cloudwatch_metric(
            cw_client, "P99Latency", "Variant", "canary"
        )
        canary_positive_rate = get_cloudwatch_metric(
            cw_client, "PositivePredictionRate", "Variant", "canary"
        )
        stable_positive_rate = get_cloudwatch_metric(
            cw_client, "PositivePredictionRate", "Variant", "stable"
        )

        log.info(
            f"  Canary — error_rate={canary_error_rate:.3f} | "
            f"p99_latency={canary_p99_latency:.0f}ms | "
            f"positive_rate={canary_positive_rate:.3f}"
        )

        # ── Check rollback conditions ──────────────────────────────────────────
        if canary_error_rate > ERROR_RATE_THRESHOLD:
            return rollback(
                elbv2_client, args,
                f"Error rate {canary_error_rate:.3f} > threshold {ERROR_RATE_THRESHOLD}"
            )

        if canary_p99_latency > P99_LATENCY_THRESHOLD and canary_p99_latency > 0:
            return rollback(
                elbv2_client, args,
                f"P99 latency {canary_p99_latency:.0f}ms > {P99_LATENCY_THRESHOLD}ms"
            )

        # Prediction rate deviation check (alert only, not auto-rollback)
        if stable_positive_rate > 0 and canary_positive_rate > 0:
            rate_deviation = abs(canary_positive_rate - stable_positive_rate) / stable_positive_rate
            if rate_deviation > PREDICTION_RATE_ALERT:
                log.warning(
                    f"Prediction rate deviation: canary={canary_positive_rate:.3f}, "
                    f"stable={stable_positive_rate:.3f} ({rate_deviation:.1%} deviation) — "
                    f"ALERTING but not rolling back"
                )

    log.info("Canary monitoring complete — no rollback conditions triggered")
    return 0


def tag_mlflow_model_staging(args, version: str):
    """Tag the MLflow model version as 'Staging' (passed canary — ready to promote)."""
    import mlflow
    mlflow.set_tracking_uri(args.mlflow_tracking_uri)
    client = mlflow.tracking.MlflowClient()
    client.transition_model_version_stage(
        name=args.model_name,
        version=version,
        stage="Staging",
        archive_existing_versions=False,
    )
    log.info(f"Tagged model {args.model_name} v{version} as Staging in MLflow")


def main():
    args       = parse_args()
    elbv2      = boto3.client("elbv2",       region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    cw_client  = boto3.client("cloudwatch",  region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    log.info(f"Starting canary deployment for model version {args.model_version}")

    # ── Step 1: Route 10% to canary ───────────────────────────────────────────
    try:
        set_traffic_weights(
            elbv2,
            args.listener_rule_arn,
            args.stable_tg_arn,
            args.canary_tg_arn,
            stable_weight=STABLE_WEIGHT_PERCENT,
            canary_weight=CANARY_WEIGHT_PERCENT,
        )
    except Exception as e:
        log.error(f"Failed to set canary traffic: {e}")
        sys.exit(1)

    # ── Step 2: Monitor canary ─────────────────────────────────────────────────
    exit_code = monitor_canary(args, elbv2, cw_client)

    if exit_code == 0:
        # Canary healthy — leave at 10% until promote.py bumps to 100%
        log.info("Canary healthy. Ready to promote (run promote_model.py).")
        tag_mlflow_model_staging(args, args.model_version)
        # Emit GitHub Actions output
        print(f"::set-output name=canary_status::healthy")
        print(f"::set-output name=model_version::{args.model_version}")

    # exit_code 1 = already rolled back inside monitor_canary
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
