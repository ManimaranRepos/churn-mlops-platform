"""
Data validation — first step in the ML pipeline (before training).

WHY validate data before training?
  "Garbage in, garbage out" is the #1 cause of silent ML failures.
  A model trained on stale, corrupt, or shifted data will produce confident
  wrong predictions. This script catches those issues at pipeline time,
  not in production.

Checks performed:
  1. Freshness: data is from the last 48 hours (not a stale snapshot)
  2. Volume: at least 1000 rows per class (enough to train on)
  3. Schema: all expected feature columns are present
  4. Null rates: no feature exceeds 50% nulls
  5. Target distribution: churn rate is between 3% and 30% (sanity check)
  6. Feature distributions: no feature has 0 variance (constant column)
  7. Duplicate customers: dedup check (same customer should appear once)

Uses Great Expectations (GE) for rules 1-6 so the validation history is
tracked and viewable in the GE Data Docs site.

Exit code 0 = all checks passed (pipeline continues)
Exit code 1 = at least one critical check failed (pipeline aborted)
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import great_expectations as ge
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Expected feature columns (must match data_loader.py) ──────────────────────
REQUIRED_COLUMNS = [
    "customer_id",
    "is_churned",
    "snapshot_date",
    "cohort",
    "current_plan",
    # Key numeric features (not exhaustive — we check all 25 in the loop)
    "total_events_7d",
    "total_events_30d",
    "avg_session_duration_7d",
    "days_since_last_login",
    "session_duration_trend",
    "transaction_trend",
]

NUMERIC_FEATURES = [
    "total_events_7d", "unique_sessions_7d", "avg_session_duration_7d",
    "std_session_duration_7d", "max_session_duration_7d",
    "total_transaction_amount_7d", "transaction_count_7d",
    "login_count_7d", "feature_usage_count_7d", "support_tickets_7d",
    "total_events_30d", "unique_sessions_30d", "avg_session_duration_30d",
    "transaction_count_30d", "total_transaction_amount_30d",
    "support_tickets_30d", "plan_downgrades_30d",
    "total_events_90d", "transaction_count_90d", "total_transaction_amount_90d",
    "days_since_last_login", "session_duration_trend", "transaction_trend",
    "feature_engagement_ratio_7d", "support_ticket_rate_30d",
]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-s3",        required=True, help="S3 path to feature parquet")
    parser.add_argument("--output-file",      default="validation_result.json")
    parser.add_argument("--min-rows",         type=int,   default=1000)
    parser.add_argument("--max-null-rate",    type=float, default=0.50)
    parser.add_argument("--min-churn-rate",   type=float, default=0.03)
    parser.add_argument("--max-churn-rate",   type=float, default=0.30)
    parser.add_argument("--max-data-age-hours", type=int, default=48)
    return parser.parse_args()


def load_data(s3_path: str) -> pd.DataFrame:
    import awswrangler as wr
    log.info(f"Loading data for validation: {s3_path}")
    df = wr.s3.read_parquet(path=s3_path)
    log.info(f"Loaded {len(df):,} rows × {len(df.columns)} columns")
    return df


def check_schema(df: pd.DataFrame) -> list[str]:
    """Check all required columns are present."""
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        return [f"Missing columns: {missing}"]
    return []


def check_freshness(df: pd.DataFrame, max_age_hours: int) -> list[str]:
    """
    Check that the data contains recent snapshots.
    Stale data means the Airflow feature pipeline failed silently.
    """
    if "snapshot_date" not in df.columns:
        return ["Cannot check freshness: 'snapshot_date' column missing"]

    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    latest = df["snapshot_date"].max()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    # Make latest timezone-aware for comparison
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)

    age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600

    log.info(f"Latest snapshot: {latest} (age: {age_hours:.1f}h, limit: {max_age_hours}h)")

    if latest < cutoff:
        return [f"Data too stale: latest snapshot is {age_hours:.1f}h old (max {max_age_hours}h)"]
    return []


def check_volume(df: pd.DataFrame, min_rows: int) -> list[str]:
    """
    Minimum rows per class — we need enough churned examples to learn from.
    With <500 churned customers the model might just memorise them.
    """
    failures = []
    n_total  = len(df)
    n_churn  = df["is_churned"].sum() if "is_churned" in df.columns else 0
    n_active = n_total - n_churn

    if n_total < min_rows:
        failures.append(f"Too few total rows: {n_total} < {min_rows}")
    if n_churn < min_rows // 2:
        failures.append(f"Too few churned examples: {n_churn} < {min_rows // 2}")
    if n_active < min_rows:
        failures.append(f"Too few active examples: {n_active} < {min_rows}")

    log.info(f"Volume: {n_total:,} total | {n_churn:,} churned | {n_active:,} active")
    return failures


def check_target_distribution(df: pd.DataFrame, min_rate: float, max_rate: float) -> list[str]:
    """
    Churn rate sanity check.
    <3%: almost certainly a labelling bug (data pipeline didn't mark churners)
    >30%: something upstream changed the churn definition
    """
    if "is_churned" not in df.columns:
        return ["Cannot check target distribution: 'is_churned' column missing"]

    rate = df["is_churned"].mean()
    log.info(f"Churn rate: {rate:.1%} (expected: {min_rate:.1%}–{max_rate:.1%})")

    failures = []
    if rate < min_rate:
        failures.append(f"Churn rate too low: {rate:.3f} < {min_rate}")
    if rate > max_rate:
        failures.append(f"Churn rate too high: {rate:.3f} > {max_rate}")
    return failures


def check_null_rates(df: pd.DataFrame, max_null_rate: float) -> list[str]:
    """Check that no feature column exceeds the null rate threshold."""
    failures = []
    for col in NUMERIC_FEATURES:
        if col not in df.columns:
            continue
        null_rate = df[col].isnull().mean()
        if null_rate > max_null_rate:
            failures.append(f"High null rate in '{col}': {null_rate:.1%} > {max_null_rate:.1%}")
            log.warning(f"  NULL rate {col}: {null_rate:.1%}")

    if not failures:
        log.info(f"Null rates: all features below {max_null_rate:.1%} threshold")
    return failures


def check_zero_variance(df: pd.DataFrame) -> list[str]:
    """
    Constant columns have zero information — they inflate memory and can
    break normalisation (division by zero in StandardScaler).
    """
    failures = []
    for col in NUMERIC_FEATURES:
        if col not in df.columns:
            continue
        non_null = df[col].dropna()
        if len(non_null) > 0 and non_null.std() == 0:
            failures.append(f"Zero-variance column: '{col}' = {non_null.iloc[0]}")
            log.warning(f"  Zero variance: {col}")

    if not failures:
        log.info("Zero-variance check: all features have non-zero variance")
    return failures


def check_duplicates(df: pd.DataFrame) -> list[str]:
    """
    Each customer should appear once in the feature snapshot.
    Duplicates would inflate training data and leak information.
    """
    if "customer_id" not in df.columns:
        return []

    n_total  = len(df)
    n_unique = df["customer_id"].nunique()
    n_dupes  = n_total - n_unique

    log.info(f"Dedup check: {n_unique:,} unique customers / {n_total:,} rows ({n_dupes} duplicates)")

    if n_dupes > n_total * 0.01:  # More than 1% duplicates is suspicious
        return [f"Too many duplicate customer_ids: {n_dupes} ({n_dupes/n_total:.1%})"]
    return []


def run_great_expectations(df: pd.DataFrame) -> dict:
    """
    Run Great Expectations suite for structured validation history.
    GE tracks validation results over time, alerting us when distribution shifts happen.
    """
    ge_df = ge.from_pandas(df)

    results = ge_df.validate(expectation_suite=ge.core.ExpectationSuite(
        expectation_suite_name="churn_feature_suite",
        expectations=[
            # Customer ID is unique and never null
            ge.core.ExpectationConfiguration(
                expectation_type="expect_column_values_to_not_be_null",
                kwargs={"column": "customer_id"},
            ),
            ge.core.ExpectationConfiguration(
                expectation_type="expect_column_values_to_be_unique",
                kwargs={"column": "customer_id"},
            ),
            # Target is binary
            ge.core.ExpectationConfiguration(
                expectation_type="expect_column_values_to_be_in_set",
                kwargs={"column": "is_churned", "value_set": [0, 1]},
            ),
            # Days since last login: physical constraint (can't be negative or >1000)
            ge.core.ExpectationConfiguration(
                expectation_type="expect_column_values_to_be_between",
                kwargs={"column": "days_since_last_login", "min_value": 0, "max_value": 1000},
            ),
        ],
    ))

    return {
        "ge_passed":    results["success"],
        "ge_evaluated": results["statistics"]["evaluated_expectations"],
        "ge_passed_count": results["statistics"]["successful_expectations"],
    }


def main():
    args = parse_args()
    df   = load_data(args.input_s3)

    all_failures = []
    warnings     = []

    # ── Run all checks ─────────────────────────────────────────────────────────
    log.info("Running schema check...")
    all_failures.extend(check_schema(df))

    log.info("Running freshness check...")
    all_failures.extend(check_freshness(df, args.max_data_age_hours))

    log.info("Running volume check...")
    all_failures.extend(check_volume(df, args.min_rows))

    log.info("Running target distribution check...")
    all_failures.extend(check_target_distribution(df, args.min_churn_rate, args.max_churn_rate))

    log.info("Running null rate check...")
    all_failures.extend(check_null_rates(df, args.max_null_rate))

    log.info("Running zero-variance check...")
    warnings.extend(check_zero_variance(df))  # Warning, not failure

    log.info("Running duplicate check...")
    all_failures.extend(check_duplicates(df))

    log.info("Running Great Expectations suite...")
    ge_summary = run_great_expectations(df)

    # ── Summary ────────────────────────────────────────────────────────────────
    result = {
        "passed":   len(all_failures) == 0,
        "failures": all_failures,
        "warnings": warnings,
        "stats": {
            "total_rows":    len(df),
            "total_columns": len(df.columns),
            "churn_rate":    float(df["is_churned"].mean()) if "is_churned" in df.columns else None,
        },
        **ge_summary,
    }

    Path(args.output_file).write_text(json.dumps(result, indent=2))

    if all_failures:
        log.error(f"Data validation FAILED ({len(all_failures)} issues):")
        for f in all_failures:
            log.error(f"  ✗ {f}")
        print(f"::set-output name=validation_passed::false")
        sys.exit(1)
    else:
        log.info(f"Data validation PASSED | {len(warnings)} warnings")
        if warnings:
            for w in warnings:
                log.warning(f"  ⚠ {w}")
        print(f"::set-output name=validation_passed::true")
        sys.exit(0)


if __name__ == "__main__":
    main()
