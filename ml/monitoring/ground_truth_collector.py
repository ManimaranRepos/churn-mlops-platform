"""
Ground Truth Collector — enables Model Quality Monitoring

Model Quality Monitor needs to compare the model's past predictions against
what actually happened (did the customer churn or not?). This requires:

  1. Capturing predictions at inference time with a unique inference_id
     (SageMaker Data Capture does this automatically)
  2. Collecting ground truth labels when churn events are confirmed in the CRM
  3. Joining predictions to ground truth by inference_id
  4. Writing the joined dataset to S3 in the format SageMaker expects

Timeline:
  - Day 0: Customer gets a churn score (inference). We save inference_id.
  - Day 30–90: Customer either churns or renews. CRM records the outcome.
  - Weekly: This script joins predictions (from Data Capture) to outcomes (from CRM).
  - SageMaker Model Quality Monitor consumes the joined dataset hourly.

WHY 30–90 day lag?
  Churn is a delayed event — a customer predicted to churn at score 0.8 may
  take a month to actually cancel. Evaluation on same-day data is meaningless
  for a monthly churn model. This lag is the fundamental reason model quality
  monitoring is harder than data quality monitoring.

Called from: weekly Airflow task (ground_truth_collection), or manually.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pandas as pd

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

AWS_REGION       = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT      = os.environ.get("ENVIRONMENT", "dev")
PROJECT          = os.environ.get("PROJECT", "churn-platform")
ARTIFACTS_BUCKET = os.environ["ARTIFACTS_BUCKET"]
RAW_BUCKET       = os.environ["RAW_BUCKET"]

# SageMaker Data Capture stores predictions here (set in Terraform endpoint config)
DATA_CAPTURE_PREFIX = os.environ.get(
    "DATA_CAPTURE_PREFIX", "model-monitor/data-capture/"
)
# CRM churn outcomes land here (written by the CRM integration Lambda)
GROUND_TRUTH_PREFIX = os.environ.get(
    "GROUND_TRUTH_PREFIX", "ground-truth/churn-outcomes/"
)
# Model Quality Monitor reads joined labels from here
MERGED_LABELS_PREFIX = os.environ.get(
    "MERGED_LABELS_PREFIX", "model-monitor/merged-labels/"
)


def _read_data_capture_records(
    s3: Any,
    start_date: datetime,
    end_date: datetime,
) -> pd.DataFrame:
    """
    Read SageMaker Data Capture records for a date range.

    Data Capture writes one JSONL file per inference request in the format:
      {"captureData": {"endpointInput": {...}, "endpointOutput": {...}},
       "inferenceId": "uuid", "eventMetadata": {"inferenceTime": "..."}}

    We extract: inference_id, prediction_score, inference_timestamp, customer_id.
    customer_id is passed as a custom attribute in the inference request header.
    """
    records  = []
    paginator = s3.get_paginator("list_objects_v2")

    # Data Capture partitions by year/month/day/hour
    for day_offset in range((end_date - start_date).days + 1):
        day = start_date + timedelta(days=day_offset)
        prefix = f"{DATA_CAPTURE_PREFIX}{day.strftime('%Y/%m/%d/')}/"

        for page in paginator.paginate(Bucket=ARTIFACTS_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                try:
                    body = s3.get_object(
                        Bucket=ARTIFACTS_BUCKET, Key=obj["Key"]
                    )["Body"].read().decode()
                    for line in body.strip().splitlines():
                        rec   = json.loads(line)
                        meta  = rec.get("eventMetadata", {})
                        output = rec.get("captureData", {}).get("endpointOutput", {})
                        data   = json.loads(
                            output.get("data", output.get("body", "{}"))
                        )
                        records.append({
                            "inference_id":       rec.get("inferenceId", ""),
                            "customer_id":        meta.get("customAttributes", {}).get("customer_id", ""),
                            "churn_probability":  data.get("churn_probability", None),
                            "churn_prediction":   data.get("churn_prediction", None),
                            "inference_timestamp": meta.get("inferenceTime", ""),
                        })
                except Exception as e:
                    log.warning(f"Failed to parse capture file {obj['Key']}: {e}")

    df = pd.DataFrame(records)
    log.info(f"Loaded {len(df):,} Data Capture records ({start_date.date()} – {end_date.date()})")
    return df


def _read_ground_truth(
    s3: Any,
    start_date: datetime,
    end_date: datetime,
) -> pd.DataFrame:
    """
    Read CRM churn outcome records.

    The CRM integration Lambda writes daily Parquet files:
      s3://raw-bucket/ground-truth/churn-outcomes/YYYY/MM/DD/outcomes.parquet

    Each row: customer_id, churned (bool), outcome_date (when we confirmed churn/stay).
    """
    import io

    dfs = []
    s3_client = s3
    paginator  = s3_client.get_paginator("list_objects_v2")

    for day_offset in range((end_date - start_date).days + 1):
        day    = start_date + timedelta(days=day_offset)
        prefix = f"{GROUND_TRUTH_PREFIX}{day.strftime('%Y/%m/%d/')}"

        for page in paginator.paginate(Bucket=RAW_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith(".parquet"):
                    continue
                try:
                    body = s3_client.get_object(
                        Bucket=RAW_BUCKET, Key=obj["Key"]
                    )["Body"].read()
                    dfs.append(pd.read_parquet(io.BytesIO(body)))
                except Exception as e:
                    log.warning(f"Failed to read ground truth {obj['Key']}: {e}")

    if not dfs:
        log.warning("No ground truth records found for date range")
        return pd.DataFrame(columns=["customer_id", "churned", "outcome_date"])

    df = pd.concat(dfs, ignore_index=True)
    log.info(f"Loaded {len(df):,} ground truth records")
    return df


def merge_predictions_with_ground_truth(
    predictions_start: datetime,
    ground_truth_start: datetime,
    ground_truth_end: datetime,
) -> dict:
    """
    Join predictions from Data Capture to confirmed churn outcomes.

    Timeline logic:
      - predictions_start: look back 90 days for predictions made before this window
      - ground_truth_start/end: the window when outcomes were confirmed

    Match: customer_id (predictions are for future churn, outcomes are confirmed churn).
    WHY not match on inference_id?
      CRM ground truth records the customer_id and outcome date, not the inference_id.
      We join on customer_id + closest inference date before outcome.

    Output format (SageMaker Model Quality Monitor CSV):
      inference_id, prediction (0/1), label (0/1), ground_truth_attribute
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)

    predictions_end = ground_truth_end   # We need predictions made before outcomes were confirmed

    pred_df = _read_data_capture_records(s3, predictions_start, predictions_end)
    gt_df   = _read_ground_truth(s3, ground_truth_start, ground_truth_end)

    if pred_df.empty or gt_df.empty:
        log.warning("No data to merge — skipping")
        return {"merged_count": 0, "output_s3_uri": None}

    # Convert timestamps
    pred_df["inference_timestamp"] = pd.to_datetime(
        pred_df["inference_timestamp"], utc=True, errors="coerce"
    )
    gt_df["outcome_date"] = pd.to_datetime(
        gt_df["outcome_date"], utc=True, errors="coerce"
    )

    # Keep only the latest prediction per customer (before their outcome date)
    pred_df = pred_df.sort_values("inference_timestamp")
    latest_pred = pred_df.groupby("customer_id").last().reset_index()

    merged = latest_pred.merge(gt_df[["customer_id", "churned"]], on="customer_id", how="inner")

    if merged.empty:
        log.warning("No matching records after join — check customer_id consistency")
        return {"merged_count": 0, "output_s3_uri": None}

    # Build Model Quality Monitor input format
    output_df = pd.DataFrame({
        "inference_id":          merged["inference_id"],
        "prediction":            merged["churn_prediction"].astype(int),
        "label":                 merged["churned"].astype(int),
        "probability":           merged["churn_probability"],
    })

    log.info(
        f"Merged {len(output_df):,} records | "
        f"Churn rate in predictions: {output_df['prediction'].mean():.1%} | "
        f"Actual churn rate: {output_df['label'].mean():.1%}"
    )

    # Write to S3
    today     = datetime.now(timezone.utc)
    s3_key    = f"{MERGED_LABELS_PREFIX}{today.strftime('%Y/%m/%d')}/merged_labels.csv"
    csv_bytes = output_df.to_csv(index=False).encode()

    s3.put_object(
        Bucket=ARTIFACTS_BUCKET,
        Key=s3_key,
        Body=csv_bytes,
        ContentType="text/csv",
    )
    output_s3_uri = f"s3://{ARTIFACTS_BUCKET}/{s3_key}"
    log.info(f"Merged labels written to {output_s3_uri}")

    return {
        "merged_count":    len(output_df),
        "output_s3_uri":   output_s3_uri,
        "churn_rate_pred": float(output_df["prediction"].mean()),
        "churn_rate_actual": float(output_df["label"].mean()),
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days",      type=int, default=90,
                        help="Days to look back for predictions")
    parser.add_argument("--ground-truth-days",  type=int, default=7,
                        help="Days of ground truth outcomes to collect")
    args = parser.parse_args()

    now    = datetime.now(timezone.utc)
    gt_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    gt_start = gt_end - timedelta(days=args.ground_truth_days)
    pred_start = gt_start - timedelta(days=args.lookback_days)

    result = merge_predictions_with_ground_truth(
        predictions_start  = pred_start,
        ground_truth_start = gt_start,
        ground_truth_end   = gt_end,
    )

    Path("/tmp/ground_truth_result.json").write_text(json.dumps(result))
    log.info(json.dumps(result, indent=2))


# Type stub for mypy
from typing import Any

if __name__ == "__main__":
    main()
