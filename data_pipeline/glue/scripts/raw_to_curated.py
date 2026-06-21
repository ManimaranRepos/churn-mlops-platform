"""
Glue ETL: Raw JSON → Curated Apache Iceberg Tables
====================================================
Runs daily at 2am. Reads incremental raw events from S3,
applies type casting and quality rules, and writes to Iceberg.

Why Iceberg?
  - Time-travel: SELECT * FROM events FOR SYSTEM_TIME AS OF '2024-01-01'
  - Schema evolution: ALTER TABLE ADD COLUMN without rewriting data
  - ACID transactions: concurrent reads/writes don't corrupt the table
  - Partition evolution: change partitioning strategy without data migration

Glue job bookmark ensures only NEW data is processed each run.
This script is idempotent — safe to re-run on the same partition.
"""

import sys
from datetime import datetime, timezone, timedelta

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.functions import (
    col, from_json, to_timestamp, year, month, dayofmonth, hour,
    when, lit, coalesce, regexp_replace, lower, trim, abs as spark_abs,
)

# ── Job bootstrap ─────────────────────────────────────────────────────────────
args = getResolvedOptions(sys.argv, [
    "JOB_NAME",
    "RAW_BUCKET",
    "PROCESSED_BUCKET",
    "GLUE_DATABASE",
    "AWS_REGION",
])

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args["JOB_NAME"], args)

# ── Iceberg configuration ─────────────────────────────────────────────────────
# These conf values activate the Iceberg catalog backed by Glue Data Catalog.
# Already set in the Terraform job default_arguments["--conf"] but we set
# here too for clarity and local testing compatibility.
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

RAW_BUCKET       = args["RAW_BUCKET"]
PROCESSED_BUCKET = args["PROCESSED_BUCKET"]
DATABASE         = args["GLUE_DATABASE"]

# Process events from the last 25 hours (1hr overlap to catch late arrivals)
NOW     = datetime.now(timezone.utc)
CUTOFF  = NOW - timedelta(hours=25)

print(f"Processing events from {CUTOFF.isoformat()} to {NOW.isoformat()}")

# ── Schema for incoming raw JSON ───────────────────────────────────────────────
# Matching the schema from event_schemas.py in the producer
RAW_SCHEMA = T.StructType([
    T.StructField("event_id",           T.StringType(),  nullable=False),
    T.StructField("customer_id",        T.StringType(),  nullable=False),
    T.StructField("event_type",         T.StringType(),  nullable=False),
    T.StructField("timestamp",          T.StringType(),  nullable=True),
    T.StructField("device",             T.StringType(),  nullable=True),
    T.StructField("session_id",         T.StringType(),  nullable=True),
    T.StructField("session_duration",   T.IntegerType(), nullable=True),
    T.StructField("transaction_amount", T.DoubleType(),  nullable=True),
    T.StructField("feature_flags",      T.StringType(),  nullable=True),  # JSON string
    T.StructField("cohort",             T.StringType(),  nullable=True),
    T.StructField("plan",               T.StringType(),  nullable=True),
    T.StructField("customer_state",     T.StringType(),  nullable=True),
    T.StructField("metadata",           T.StringType(),  nullable=True),  # JSON string
])

FEATURE_FLAGS_SCHEMA = T.MapType(T.StringType(), T.BooleanType())

# ── Step 1: Read raw Parquet from S3 ─────────────────────────────────────────
print("Reading raw events from S3...")

# Read only recent partitions using path filters — avoids full table scan
raw_paths = [
    f"s3://{RAW_BUCKET}/events/year={CUTOFF.year}/month={CUTOFF.month:02d}/day={CUTOFF.day:02d}/",
    f"s3://{RAW_BUCKET}/events/year={NOW.year}/month={NOW.month:02d}/day={NOW.day:02d}/",
]

raw_df = (
    spark.read
    .option("mergeSchema", "true")   # Handle schema evolution in raw files
    .parquet(*raw_paths)
)

print(f"Raw record count: {raw_df.count():,}")

# ── Step 2: Parse and cast fields ─────────────────────────────────────────────
print("Applying transformations...")

curated_df = (
    raw_df

    # Deduplicate: same event_id can arrive twice if Firehose retried
    # Use dropDuplicates on event_id (the natural dedup key)
    .dropDuplicates(["event_id"])

    # Timestamp: parse ISO 8601 to proper timestamp type
    .withColumn(
        "event_timestamp",
        to_timestamp(col("timestamp"), "yyyy-MM-dd'T'HH:mm:ssXXX")
    )

    # Filter out events outside our processing window
    .filter(col("event_timestamp") >= lit(CUTOFF.isoformat()))
    .filter(col("event_timestamp") <= lit(NOW.isoformat()))

    # Normalise string fields: lowercase, trim whitespace
    .withColumn("event_type",     lower(trim(col("event_type"))))
    .withColumn("device",         lower(trim(col("device"))))
    .withColumn("plan",           lower(trim(col("plan"))))
    .withColumn("customer_state", lower(trim(col("customer_state"))))

    # Validate customer_id: must not be empty or 'anonymous'
    .withColumn("customer_id",
        when(
            col("customer_id").isNotNull() &
            (col("customer_id") != "") &
            (col("customer_id") != "anonymous"),
            col("customer_id")
        ).otherwise(None)
    )

    # Clamp session_duration: negative values and outliers are data quality issues
    # Max 24 hours (86400 sec) — anything longer is likely a bug in the producer
    .withColumn("session_duration",
        when(col("session_duration") < 0, lit(0))
        .when(col("session_duration") > 86400, lit(86400))
        .otherwise(col("session_duration"))
    )

    # Clamp transaction_amount: no negative transactions (refunds are separate events)
    .withColumn("transaction_amount",
        when(col("transaction_amount") < 0, lit(0.0))
        .when(col("transaction_amount") > 100000, lit(None).cast(T.DoubleType()))
        .otherwise(col("transaction_amount"))
    )

    # Parse feature_flags JSON string → typed map
    .withColumn(
        "feature_flags_parsed",
        from_json(col("feature_flags"), FEATURE_FLAGS_SCHEMA)
    )

    # Add partition columns for Iceberg hidden partitioning
    .withColumn("event_year",  year(col("event_timestamp")))
    .withColumn("event_month", month(col("event_timestamp")))
    .withColumn("event_day",   dayofmonth(col("event_timestamp")))
    .withColumn("event_hour",  hour(col("event_timestamp")))

    # Add audit columns
    .withColumn("_etl_timestamp", lit(NOW.isoformat()).cast(T.TimestampType()))
    .withColumn("_etl_job",       lit(args["JOB_NAME"]))

    # Drop rows where customer_id is null (unfixable quality issue)
    .filter(col("customer_id").isNotNull())

    # Select final columns for curated table
    .select(
        "event_id",
        "customer_id",
        "event_type",
        col("event_timestamp").alias("event_timestamp"),
        "device",
        "session_id",
        "session_duration",
        "transaction_amount",
        col("feature_flags_parsed").alias("feature_flags"),
        "cohort",
        "plan",
        "customer_state",
        "event_year",
        "event_month",
        "event_day",
        "event_hour",
        "_etl_timestamp",
        "_etl_job",
    )
)

record_count = curated_df.count()
print(f"Curated record count after dedup + quality filters: {record_count:,}")

# ── Step 3: Write to Iceberg ───────────────────────────────────────────────────
print("Writing to Iceberg table...")

ICEBERG_TABLE = f"glue_catalog.{DATABASE}.events"

# Create table if it doesn't exist (idempotent)
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {ICEBERG_TABLE} (
        event_id           STRING        NOT NULL COMMENT 'Unique event identifier',
        customer_id        STRING        NOT NULL COMMENT 'Customer identifier',
        event_type         STRING        NOT NULL COMMENT 'Event classification',
        event_timestamp    TIMESTAMP     NOT NULL COMMENT 'When the event occurred',
        device             STRING                 COMMENT 'Device type: mobile/desktop/tablet',
        session_id         STRING                 COMMENT 'User session identifier',
        session_duration   INT                    COMMENT 'Session length in seconds',
        transaction_amount DOUBLE                 COMMENT 'Transaction value in USD',
        feature_flags      MAP<STRING, BOOLEAN>   COMMENT 'Feature flag states at event time',
        cohort             STRING                 COMMENT 'Customer acquisition cohort (YYYY-QN)',
        plan               STRING                 COMMENT 'Subscription plan',
        customer_state     STRING                 COMMENT 'Lifecycle state (label for ML)',
        event_year         INT                    COMMENT 'Partition: year',
        event_month        INT                    COMMENT 'Partition: month',
        event_day          INT                    COMMENT 'Partition: day',
        event_hour         INT                    COMMENT 'Partition: hour',
        _etl_timestamp     TIMESTAMP              COMMENT 'When this record was ETL-processed',
        _etl_job           STRING                 COMMENT 'Glue job that wrote this record'
    )
    USING iceberg
    PARTITIONED BY (event_year, event_month, event_day)
    LOCATION 's3://{PROCESSED_BUCKET}/iceberg/{DATABASE}/events'
    TBLPROPERTIES (
        'write.format.default'               = 'parquet',
        'write.parquet.compression-codec'   = 'snappy',
        -- Compaction: merge small files automatically
        'write.target-file-size-bytes'      = '134217728',
        -- Retain 30 snapshots (for time-travel up to 30 ETL runs back)
        'history.expire.max-snapshot-age-ms' = '2592000000',
        'history.expire.min-snapshots-to-keep' = '30'
    )
""")

# MERGE (upsert): insert new records, update if event_id already exists
# This handles late-arriving events and Firehose retries gracefully
curated_df.createOrReplaceTempView("incoming_events")

spark.sql(f"""
    MERGE INTO {ICEBERG_TABLE} AS target
    USING incoming_events AS source
    ON target.event_id = source.event_id
    WHEN MATCHED AND target._etl_timestamp < source._etl_timestamp THEN
        -- Update only if incoming record is newer (idempotent re-runs)
        UPDATE SET *
    WHEN NOT MATCHED THEN
        INSERT *
""")

print(f"Merge complete. Wrote {record_count:,} events to {ICEBERG_TABLE}")

# ── Step 4: Table maintenance ─────────────────────────────────────────────────
# Run periodically to keep performance optimal.
# expire_snapshots: removes old snapshots from the manifest
# rewrite_data_files: compacts small files left by incremental writes
print("Running table maintenance...")

spark.sql(f"""
    CALL glue_catalog.system.expire_snapshots(
        table => '{DATABASE}.events',
        older_than => TIMESTAMP '{(NOW - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")}',
        retain_last => 10
    )
""")

spark.sql(f"""
    CALL glue_catalog.system.rewrite_data_files(
        table => '{DATABASE}.events',
        strategy => 'sort',
        sort_order => 'customer_id ASC, event_timestamp ASC',
        options => map('target-file-size-bytes', '134217728')
    )
""")

print("Table maintenance complete.")

# ── Step 5: Log data quality metrics to CloudWatch ───────────────────────────
import boto3

cw = boto3.client("cloudwatch", region_name=args["AWS_REGION"])

metrics = [
    {"MetricName": "RecordsProcessed",   "Value": float(record_count),     "Unit": "Count"},
    {"MetricName": "RawRecordsRead",     "Value": float(raw_df.count()),   "Unit": "Count"},
]

# Null rate for customer_id — key quality metric
null_customer_count = raw_df.filter(col("customer_id").isNull()).count()
if raw_df.count() > 0:
    null_rate = null_customer_count / raw_df.count()
    metrics.append({
        "MetricName": "NullCustomerIdRate",
        "Value": null_rate,
        "Unit": "None",
    })

cw.put_metric_data(
    Namespace="ChurnPlatform/DataQuality",
    MetricData=[
        {**m, "Dimensions": [
            {"Name": "Environment", "Value": args.get("ENVIRONMENT", "dev")},
            {"Name": "GlueJob",     "Value": args["JOB_NAME"]},
        ]}
        for m in metrics
    ],
)

print("Data quality metrics published to CloudWatch.")
job.commit()
print("Glue job complete.")
