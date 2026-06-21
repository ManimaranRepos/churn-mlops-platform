"""
Glue ETL: Feature Engineering for ML Training
===============================================
Reads from the curated Iceberg events table and computes
aggregate features per customer over rolling time windows.
These features are what the XGBoost/PyTorch churn model trains on.

Feature windows:
  - 7-day  (recent behaviour — strongest churn signal)
  - 30-day (medium-term trends)
  - 90-day (baseline behaviour for comparison)

Output: customer_features Iceberg table, partitioned by snapshot_date.
        One row per customer per day — ML training reads the latest snapshot.
"""

import sys
from datetime import datetime, timezone, timedelta

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql import Window

args = getResolvedOptions(sys.argv, [
    "JOB_NAME", "PROCESSED_BUCKET", "GLUE_DATABASE", "AWS_REGION",
])

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args["JOB_NAME"], args)

spark.conf.set("spark.sql.extensions",
    "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
spark.conf.set("spark.sql.catalog.glue_catalog",
    "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.glue_catalog.catalog-impl",
    "org.apache.iceberg.aws.glue.GlueCatalog")
spark.conf.set("spark.sql.catalog.glue_catalog.warehouse",
    f"s3://{args['PROCESSED_BUCKET']}/iceberg/")
spark.conf.set("spark.sql.catalog.glue_catalog.io-impl",
    "org.apache.iceberg.aws.s3.S3FileIO")

DATABASE   = args["GLUE_DATABASE"]
BUCKET     = args["PROCESSED_BUCKET"]
EVENTS_TBL = f"glue_catalog.{DATABASE}.events"
FEATS_TBL  = f"glue_catalog.{DATABASE}.customer_features"
NOW        = datetime.now(timezone.utc)
TODAY      = NOW.date()

print(f"Computing features as of {TODAY}")

# ── Read events ───────────────────────────────────────────────────────────────
events_df = spark.table(EVENTS_TBL).filter(
    F.col("event_timestamp") >= F.lit((NOW - timedelta(days=91)).isoformat())
)

events_df.cache()

# ── Helper: aggregate over N-day window ───────────────────────────────────────
def compute_window_features(df, days: int, suffix: str):
    """Aggregate customer behaviour over the last `days` days."""
    cutoff = NOW - timedelta(days=days)
    window_df = df.filter(F.col("event_timestamp") >= F.lit(cutoff.isoformat()))

    return window_df.groupBy("customer_id").agg(
        # Volume signals
        F.count("*").alias(f"total_events_{suffix}"),
        F.countDistinct("session_id").alias(f"unique_sessions_{suffix}"),

        # Session behaviour
        F.mean("session_duration").alias(f"avg_session_duration_{suffix}"),
        F.stddev("session_duration").alias(f"std_session_duration_{suffix}"),
        F.max("session_duration").alias(f"max_session_duration_{suffix}"),

        # Transaction behaviour
        F.sum("transaction_amount").alias(f"total_transaction_amount_{suffix}"),
        F.count(F.when(F.col("transaction_amount") > 0, 1)).alias(f"transaction_count_{suffix}"),
        F.mean("transaction_amount").alias(f"avg_transaction_amount_{suffix}"),

        # Event type distribution (key churn signals)
        F.count(F.when(F.col("event_type") == "login", 1)).alias(f"login_count_{suffix}"),
        F.count(F.when(F.col("event_type") == "feature_usage", 1)).alias(f"feature_usage_count_{suffix}"),
        F.count(F.when(F.col("event_type") == "support_ticket", 1)).alias(f"support_tickets_{suffix}"),
        F.count(F.when(F.col("event_type") == "plan_downgrade", 1)).alias(f"plan_downgrades_{suffix}"),

        # Recency
        F.max("event_timestamp").alias(f"last_event_timestamp_{suffix}"),
        F.min("event_timestamp").alias(f"first_event_timestamp_{suffix}"),

        # Feature engagement
        F.countDistinct("device").alias(f"device_types_{suffix}"),
    )


# ── Compute features for each window ─────────────────────────────────────────
print("Computing 7-day features...")
features_7d  = compute_window_features(events_df, 7,  "7d")
print("Computing 30-day features...")
features_30d = compute_window_features(events_df, 30, "30d")
print("Computing 90-day features...")
features_90d = compute_window_features(events_df, 90, "90d")

# ── Customer-level static features ───────────────────────────────────────────
static_features = events_df.groupBy("customer_id").agg(
    F.last("cohort",         ignorenulls=True).alias("cohort"),
    F.last("plan",           ignorenulls=True).alias("current_plan"),
    F.last("customer_state", ignorenulls=True).alias("customer_state"),
    F.max("event_timestamp").alias("last_seen_at"),
    F.min("event_timestamp").alias("first_seen_at"),
    F.count("*").alias("total_events_all_time"),
)

# ── Join all feature sets ─────────────────────────────────────────────────────
print("Joining feature windows...")
customer_features = (
    static_features
    .join(features_7d,  on="customer_id", how="left")
    .join(features_30d, on="customer_id", how="left")
    .join(features_90d, on="customer_id", how="left")
)

# ── Derived features (ratios and trends) ─────────────────────────────────────
# Trend features are often more predictive than absolute values:
# "customer's session duration dropped 50% this week" > "session_duration = 120s"
customer_features = customer_features.withColumns({
    # Days since last login (recency signal)
    "days_since_last_login": F.datediff(
        F.lit(TODAY.isoformat()),
        F.col("last_seen_at").cast("date")
    ),

    # Session duration trend: is engagement increasing or decreasing?
    # Negative values → customer is spending less time → churn risk
    "session_duration_trend": (
        F.col("avg_session_duration_7d") - F.col("avg_session_duration_30d")
    ) / F.greatest(F.col("avg_session_duration_30d"), F.lit(1.0)),

    # Transaction frequency trend: fewer transactions → churn risk
    "transaction_trend": (
        F.col("transaction_count_7d").cast("double") / F.greatest(F.lit(7.0), F.lit(1.0))
        - F.col("transaction_count_30d").cast("double") / F.greatest(F.lit(30.0), F.lit(1.0))
    ),

    # Login to feature_usage ratio: high login / low feature_usage = passive user
    "feature_engagement_ratio_7d": F.col("feature_usage_count_7d") / F.greatest(
        F.col("total_events_7d"), F.lit(1)
    ),

    # Support ticket rate: high = frustrated customer
    "support_ticket_rate_30d": F.col("support_tickets_30d") / F.greatest(
        F.col("total_events_30d"), F.lit(1)
    ),

    # Snapshot metadata
    "snapshot_date": F.lit(TODAY.isoformat()).cast("date"),
    "_feature_version": F.lit("1.0"),
    "_computed_at": F.lit(NOW.isoformat()).cast("timestamp"),
})

# ── Binary churn label for training ──────────────────────────────────────────
# Label = 1 if customer is in 'churned' state, 0 otherwise
# This is the Y variable the ML model learns to predict
customer_features = customer_features.withColumn(
    "is_churned",
    F.when(F.col("customer_state") == "churned", F.lit(1)).otherwise(F.lit(0))
)

print(f"Feature matrix shape: {customer_features.count():,} customers")
print("Schema:")
customer_features.printSchema()

# ── Write to Iceberg feature table ───────────────────────────────────────────
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {FEATS_TBL} (
        customer_id                STRING  NOT NULL,
        snapshot_date              DATE    NOT NULL  COMMENT 'Date features were computed',
        cohort                     STRING,
        current_plan               STRING,
        customer_state             STRING,
        is_churned                 INT               COMMENT 'Training label: 1=churned 0=active',
        last_seen_at               TIMESTAMP,
        first_seen_at              TIMESTAMP,
        total_events_all_time      LONG,
        days_since_last_login      INT,
        session_duration_trend     DOUBLE            COMMENT 'Negative=declining engagement',
        transaction_trend          DOUBLE,
        feature_engagement_ratio_7d DOUBLE,
        support_ticket_rate_30d    DOUBLE,
        -- 7-day aggregates
        total_events_7d            LONG,
        unique_sessions_7d         LONG,
        avg_session_duration_7d    DOUBLE,
        std_session_duration_7d    DOUBLE,
        max_session_duration_7d    DOUBLE,
        total_transaction_amount_7d DOUBLE,
        transaction_count_7d       LONG,
        login_count_7d             LONG,
        feature_usage_count_7d     LONG,
        support_tickets_7d         LONG,
        -- 30-day aggregates
        total_events_30d           LONG,
        unique_sessions_30d        LONG,
        avg_session_duration_30d   DOUBLE,
        transaction_count_30d      LONG,
        total_transaction_amount_30d DOUBLE,
        support_tickets_30d        LONG,
        plan_downgrades_30d        LONG,
        -- 90-day aggregates
        total_events_90d           LONG,
        transaction_count_90d      LONG,
        total_transaction_amount_90d DOUBLE,
        _feature_version           STRING,
        _computed_at               TIMESTAMP
    )
    USING iceberg
    PARTITIONED BY (snapshot_date)
    LOCATION 's3://{BUCKET}/iceberg/{DATABASE}/customer_features'
    TBLPROPERTIES (
        'write.format.default'               = 'parquet',
        'write.parquet.compression-codec'   = 'snappy',
        'history.expire.min-snapshots-to-keep' = '90'
    )
""")

# Write today's snapshot (overwrite if re-running same day)
customer_features.writeTo(FEATS_TBL) \
    .overwritePartitions()

print(f"Feature table written: {FEATS_TBL}")
job.commit()
