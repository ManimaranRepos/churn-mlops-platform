"""
Data loader: reads customer feature snapshots from Athena/S3 Iceberg tables.
Used by both XGBoost and PyTorch training scripts.

Why Athena instead of reading S3 directly?
  Athena handles partition discovery and predicate pushdown automatically.
  Reading S3 Parquet directly requires manual partition path construction.
  Athena also validates the schema against the Glue catalog.
"""

import io
import logging
import os
import time
from typing import Optional

import awswrangler as wr  # AWS Data Wrangler — pandas + Athena integration
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, LabelEncoder

log = logging.getLogger(__name__)

# ── Feature columns (aligned with feature_engineering.py output) ──────────────
NUMERIC_FEATURES = [
    # 7-day window
    "total_events_7d",
    "unique_sessions_7d",
    "avg_session_duration_7d",
    "std_session_duration_7d",
    "max_session_duration_7d",
    "total_transaction_amount_7d",
    "transaction_count_7d",
    "login_count_7d",
    "feature_usage_count_7d",
    "support_tickets_7d",
    # 30-day window
    "total_events_30d",
    "unique_sessions_30d",
    "avg_session_duration_30d",
    "transaction_count_30d",
    "total_transaction_amount_30d",
    "support_tickets_30d",
    "plan_downgrades_30d",
    # 90-day window
    "total_events_90d",
    "transaction_count_90d",
    "total_transaction_amount_90d",
    # Derived/trend features
    "days_since_last_login",
    "session_duration_trend",
    "transaction_trend",
    "feature_engagement_ratio_7d",
    "support_ticket_rate_30d",
]

CATEGORICAL_FEATURES = [
    "cohort",
    "current_plan",
]

TARGET_COLUMN = "is_churned"
CUSTOMER_ID_COLUMN = "customer_id"


def load_features_from_athena(
    database: str,
    workgroup: str,
    snapshot_date: Optional[str] = None,
    lookback_days: int = 90,
    boto3_session=None,
) -> pd.DataFrame:
    """
    Load the latest customer feature snapshot from Athena.

    Args:
        database: Glue catalog database name
        workgroup: Athena workgroup to use (controls cost limits)
        snapshot_date: specific date to load (YYYY-MM-DD), defaults to latest
        lookback_days: how many days of snapshots to consider for training
        boto3_session: AWS session (uses default credentials if None)

    Returns:
        DataFrame with one row per customer, all features + target label
    """
    date_filter = (
        f"AND snapshot_date = DATE '{snapshot_date}'"
        if snapshot_date
        else f"AND snapshot_date >= DATE_ADD('day', -{lookback_days}, CURRENT_DATE)"
    )

    query = f"""
        WITH latest_per_customer AS (
            -- For each customer, get their LATEST feature snapshot
            -- (a customer may have multiple snapshots if we ran feature engineering multiple times)
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY customer_id
                       ORDER BY snapshot_date DESC
                   ) AS rn
            FROM {database}.customer_features
            WHERE 1=1
                {date_filter}
                -- Exclude customers with < 7 days of data (too few events to learn from)
                AND total_events_7d IS NOT NULL
        )
        SELECT
            customer_id,
            is_churned,
            cohort,
            current_plan,
            {", ".join(NUMERIC_FEATURES)}
        FROM latest_per_customer
        WHERE rn = 1
    """

    log.info(f"Loading features from Athena database: {database}")
    start = time.time()

    df = wr.athena.read_sql_query(
        sql=query,
        database=database,
        workgroup=workgroup,
        boto3_session=boto3_session,
        # Cache results in S3 for 1 hour — avoids re-running the same query
        # during hyperparameter tuning where the same dataset is loaded many times
        ctas_approach=True,
    )

    elapsed = time.time() - start
    log.info(
        f"Loaded {len(df):,} customer records in {elapsed:.1f}s | "
        f"Churn rate: {df[TARGET_COLUMN].mean():.1%}"
    )
    return df


def load_features_from_s3(
    s3_path: str,
    boto3_session=None,
) -> pd.DataFrame:
    """
    Load features directly from S3 Parquet (faster than Athena for SageMaker jobs
    when the data is already staged in the training input channel).
    SageMaker copies input data to /opt/ml/input/data/ before the job starts.
    """
    log.info(f"Loading features from S3: {s3_path}")

    if s3_path.startswith("s3://"):
        df = wr.s3.read_parquet(path=s3_path, boto3_session=boto3_session)
    else:
        # Local path — for /opt/ml/input/data/ inside SageMaker container
        df = pd.read_parquet(s3_path)

    log.info(f"Loaded {len(df):,} records | Churn rate: {df[TARGET_COLUMN].mean():.1%}")
    return df


def preprocess_features(
    df: pd.DataFrame,
    scaler: Optional[StandardScaler] = None,
    encoders: Optional[dict] = None,
    fit: bool = True,
) -> tuple[np.ndarray, np.ndarray, StandardScaler, dict]:
    """
    Preprocess raw features into model-ready arrays.

    Returns:
        X: feature matrix (float32)
        y: target vector (int)
        scaler: fitted StandardScaler (save for inference)
        encoders: fitted LabelEncoders per categorical (save for inference)
    """
    df = df.copy()

    # ── Fill missing values ───────────────────────────────────────────────────
    # Most NAs come from customers with no activity in a time window.
    # Fill with 0 (no activity) rather than mean (don't assume average behaviour).
    for col in NUMERIC_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna(0.0)

    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna("unknown")

    # ── Encode categoricals ───────────────────────────────────────────────────
    if encoders is None:
        encoders = {}

    encoded_cats = []
    for col in CATEGORICAL_FEATURES:
        if col not in df.columns:
            continue
        if fit:
            enc = LabelEncoder()
            encoded = enc.fit_transform(df[col].astype(str))
            encoders[col] = enc
        else:
            enc = encoders[col]
            # Handle unseen categories gracefully
            known = set(enc.classes_)
            df[col] = df[col].apply(lambda x: x if x in known else "unknown")
            encoded = enc.transform(df[col].astype(str))
        encoded_cats.append(encoded.reshape(-1, 1))

    # ── Scale numerics ────────────────────────────────────────────────────────
    numeric_values = df[NUMERIC_FEATURES].values.astype(np.float32)

    if fit:
        if scaler is None:
            scaler = StandardScaler()
        numeric_scaled = scaler.fit_transform(numeric_values)
    else:
        numeric_scaled = scaler.transform(numeric_values)

    # ── Combine numeric + encoded categorical ─────────────────────────────────
    if encoded_cats:
        X = np.hstack([numeric_scaled] + encoded_cats).astype(np.float32)
    else:
        X = numeric_scaled

    y = df[TARGET_COLUMN].values.astype(np.int32)

    log.info(
        f"Feature matrix: {X.shape} | "
        f"Class balance: {y.mean():.1%} positive (churned)"
    )
    return X, y, scaler, encoders


def split_dataset(
    X: np.ndarray,
    y: np.ndarray,
    test_size: float = 0.2,
    val_size: float = 0.1,
    random_state: int = 42,
) -> tuple:
    """
    Three-way split: train / validation / test.
    Test set is held out until final evaluation — never used during training.
    Stratified split ensures both splits have the same churn rate.
    """
    # First split off the test set
    X_temp, X_test, y_temp, y_test = train_test_split(
        X, y,
        test_size=test_size,
        stratify=y,
        random_state=random_state,
    )

    # Then split val from the remaining train data
    val_ratio = val_size / (1 - test_size)
    X_train, X_val, y_train, y_val = train_test_split(
        X_temp, y_temp,
        test_size=val_ratio,
        stratify=y_temp,
        random_state=random_state,
    )

    log.info(
        f"Dataset split — "
        f"Train: {len(X_train):,} ({y_train.mean():.1%} churn) | "
        f"Val: {len(X_val):,} ({y_val.mean():.1%} churn) | "
        f"Test: {len(X_test):,} ({y_test.mean():.1%} churn)"
    )
    return X_train, X_val, X_test, y_train, y_val, y_test
