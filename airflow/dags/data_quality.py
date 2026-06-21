"""
DAG: churn_data_quality
Schedule: Every hour at :05 past (e.g. 00:05, 01:05, ...)

Continuous data health monitoring — catches upstream issues before they silently
corrupt the daily training run.

Checks:
  1. Kinesis stream health — IncomingRecords > 0 in last 15 min (events flowing)
  2. Kinesis iterator age — GetRecords.IteratorAgeMilliseconds < 5 min (not falling behind)
  3. S3 raw data freshness — new files in last 2 hours (Firehose is flushing)
  4. Glue crawler status — last crawl completed without error
  5. Athena query health — spot-check query against curated table returns rows

WHY hourly (not per-minute)?
  These checks call CloudWatch APIs — excessive polling adds cost and hits rate limits.
  1 check/hour is enough early warning: if Kinesis stops at 09:00, we know by 10:05.
  That's a 65-minute MTTD which is acceptable for a batch training system.

WHY a separate DAG (not part of feature_pipeline)?
  Data quality runs even on days when the pipeline is paused.
  It also catches issues mid-day that won't affect today's training but WILL
  affect tomorrow's if not fixed. Separate concern → separate DAG.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import boto3
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago

from common.aws_utils import emit_dag_metric
from common.constants import (
    ARTIFACTS_BUCKET,
    ATHENA_WORKGROUP,
    AWS_REGION,
    GLUE_CRAWLER_RAW,
    GLUE_CURATED_DATABASE,
    KINESIS_STREAM_NAME,
    PROCESSED_BUCKET,
    RAW_BUCKET,
)
from common.slack_notify import on_dag_failure

log = logging.getLogger(__name__)

DEFAULT_ARGS = {
    "owner":            "ml-platform",
    "depends_on_past":  False,
    "retries":          0,            # Data quality alerts should not retry — alert once
    "on_failure_callback": on_dag_failure,
    "email_on_failure": False,
}

# Collect all check results to emit a single summary alert (not one per check)
_ISSUES: list[str] = []


def _get_cloudwatch_metric(
    client,
    metric_name: str,
    namespace: str,
    dimensions: list,
    period_seconds: int = 900,
    stat: str = "Sum",
) -> float:
    now     = datetime.now(timezone.utc)
    resp    = client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=dimensions,
        StartTime=now - timedelta(seconds=period_seconds),
        EndTime=now,
        Period=period_seconds,
        Statistics=[stat],
    )
    points  = sorted(resp.get("Datapoints", []), key=lambda x: x["Timestamp"])
    return float(points[-1][stat]) if points else 0.0


with DAG(
    dag_id="churn_data_quality",
    description="Hourly data health checks: Kinesis, S3 freshness, Glue, Athena",
    schedule_interval="5 * * * *",    # :05 past every hour
    start_date=days_ago(1),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    on_failure_callback=on_dag_failure,
    tags=["monitoring", "data-quality"],
) as dag:

    # ── Check 1: Kinesis stream health ────────────────────────────────────────
    def check_kinesis_health(**context) -> dict:
        """
        Verify the Kinesis stream is receiving events.
        IncomingRecords = 0 over the past 15 min means:
          - The event producer (Lambda/webhook) stopped sending
          - The stream was deleted/throttled
          - A deployment broke the producer

        IteratorAge > 5 min means consumers are falling behind —
          Lambda/Firehose is not keeping up with the event rate.
        """
        cw = boto3.client("cloudwatch", region_name=AWS_REGION)
        stream_dims = [{"Name": "StreamName", "Value": KINESIS_STREAM_NAME}]

        incoming = _get_cloudwatch_metric(
            cw, "IncomingRecords", "AWS/Kinesis", stream_dims,
            period_seconds=900, stat="Sum"
        )
        iterator_age_ms = _get_cloudwatch_metric(
            cw, "GetRecords.IteratorAgeMilliseconds", "AWS/Kinesis", stream_dims,
            period_seconds=900, stat="Maximum"
        )
        write_throttles = _get_cloudwatch_metric(
            cw, "WriteProvisionedThroughputExceeded", "AWS/Kinesis", stream_dims,
            period_seconds=900, stat="Sum"
        )

        log.info(
            f"Kinesis health | "
            f"IncomingRecords(15m)={incoming:.0f} | "
            f"MaxIteratorAge={iterator_age_ms/1000:.1f}s | "
            f"WriteThrottles={write_throttles:.0f}"
        )

        issues = []
        if incoming == 0:
            issues.append(f"Kinesis {KINESIS_STREAM_NAME}: no records in last 15 min")
        if iterator_age_ms > 300_000:  # 5 minutes in ms
            issues.append(f"Kinesis iterator age {iterator_age_ms/1000:.0f}s > 300s (consumers lagging)")
        if write_throttles > 100:
            issues.append(f"Kinesis write throttles: {write_throttles:.0f} (stream underprovisioned)")

        # Emit health metrics for dashboards (Phase 8)
        emit_dag_metric("KinesisIncomingRecords", incoming, "churn_data_quality", unit="Count")
        emit_dag_metric("KinesisIteratorAge",    iterator_age_ms / 1000, "churn_data_quality", unit="Seconds")

        return {"issues": issues, "incoming_records": incoming, "iterator_age_ms": iterator_age_ms}

    kinesis_check = PythonOperator(
        task_id="check_kinesis_health",
        python_callable=check_kinesis_health,
        provide_context=True,
        execution_timeout=timedelta(minutes=5),
    )

    # ── Check 2: S3 raw data freshness ────────────────────────────────────────
    def check_s3_freshness(**context) -> dict:
        """
        Verify that Firehose is writing new files to S3 raw bucket.
        We check: were any objects modified in the last 2 hours?

        WHY 2 hours (not 15 min)?
          Firehose buffers up to 5 min before writing. Plus the folder structure
          is hour-partitioned, so a check right at the top of an hour might see
          the previous hour's files as the most recent. 2 hours is a safe window.
        """
        s3     = boto3.client("s3", region_name=AWS_REGION)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

        paginator = s3.get_paginator("list_objects_v2")
        pages     = paginator.paginate(Bucket=RAW_BUCKET, Prefix="events/")

        latest_file = None
        latest_ts   = None

        for page in pages:
            for obj in page.get("Contents", []):
                if latest_ts is None or obj["LastModified"] > latest_ts:
                    latest_ts   = obj["LastModified"]
                    latest_file = obj["Key"]

        issues = []
        if latest_ts is None:
            issues.append(f"S3 raw bucket {RAW_BUCKET}/events/: no objects found")
        elif latest_ts < cutoff:
            age_hours = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
            issues.append(
                f"S3 raw data stale: latest file {age_hours:.1f}h old "
                f"(key: {latest_file})"
            )

        log.info(
            f"S3 freshness | Latest file: {latest_file} | "
            f"Age: {((datetime.now(timezone.utc) - latest_ts).total_seconds()/3600):.1f}h"
            if latest_ts else "S3 freshness | No files found"
        )

        return {"issues": issues, "latest_file": latest_file}

    s3_freshness_check = PythonOperator(
        task_id="check_s3_freshness",
        python_callable=check_s3_freshness,
        provide_context=True,
        execution_timeout=timedelta(minutes=5),
    )

    # ── Check 3: Glue crawler status ──────────────────────────────────────────
    def check_glue_crawler(**context) -> dict:
        """
        Verify the last Glue crawler run succeeded.
        A failed crawler means new partitions aren't in the Glue catalog,
        so the next Glue ETL job won't see yesterday's data.
        """
        glue   = boto3.client("glue", region_name=AWS_REGION)
        issues = []

        for crawler_name in [GLUE_CRAWLER_RAW]:
            try:
                resp    = glue.get_crawler_metrics(CrawlerNameList=[crawler_name])
                metrics = resp.get("CrawlerMetricsList", [{}])[0]

                last_runt_time = metrics.get("LastRuntimeSeconds", 0)
                tables_created = metrics.get("TablesCreated", 0)
                tables_updated = metrics.get("TablesUpdated", 0)
                tables_deleted = metrics.get("TablesDeleted", 0)

                log.info(
                    f"Crawler '{crawler_name}' | "
                    f"LastRuntime={last_runt_time}s | "
                    f"Tables: +{tables_created} ~{tables_updated} -{tables_deleted}"
                )

                # Also check crawler state
                crawler_resp = glue.get_crawler(Name=crawler_name)
                state        = crawler_resp["Crawler"]["State"]
                last_crawl   = crawler_resp["Crawler"].get("LastCrawl", {})
                status       = last_crawl.get("Status", "UNKNOWN")

                if status == "FAILED":
                    error = last_crawl.get("ErrorMessage", "no details")
                    issues.append(f"Crawler '{crawler_name}' last run FAILED: {error}")
                elif state == "RUNNING":
                    log.info(f"Crawler '{crawler_name}' currently running")

            except Exception as e:
                issues.append(f"Could not check crawler '{crawler_name}': {e}")

        return {"issues": issues}

    glue_check = PythonOperator(
        task_id="check_glue_crawler",
        python_callable=check_glue_crawler,
        provide_context=True,
        execution_timeout=timedelta(minutes=5),
    )

    # ── Check 4: Athena spot-check query ──────────────────────────────────────
    def check_athena_query(**context) -> dict:
        """
        Run a lightweight COUNT query against the curated Iceberg table.
        This validates end-to-end: Glue catalog → Iceberg → Athena query path.

        WHY COUNT (not SELECT *)?
          COUNT is a metadata-only operation on Iceberg (no data scan).
          It's essentially free — no S3 reads, no cost per row.
        """
        import awswrangler as wr

        issues  = []
        try:
            result = wr.athena.read_sql_query(
                sql=f"""
                    SELECT
                        COUNT(*) AS total_events,
                        COUNT(DISTINCT customer_id) AS unique_customers,
                        MAX(event_timestamp) AS latest_event
                    FROM {GLUE_CURATED_DATABASE}.customer_events
                    WHERE event_timestamp >= NOW() - INTERVAL '2' HOUR
                """,
                database=GLUE_CURATED_DATABASE,
                workgroup=ATHENA_WORKGROUP,
                ctas_approach=False,  # Simple query, no CTAS needed
            )

            total_events     = int(result["total_events"].iloc[0])
            unique_customers = int(result["unique_customers"].iloc[0])
            latest_event     = result["latest_event"].iloc[0]

            log.info(
                f"Athena spot-check | "
                f"Events(2h)={total_events:,} | "
                f"Customers={unique_customers:,} | "
                f"Latest={latest_event}"
            )

            if total_events == 0:
                issues.append("Athena query returned 0 events in the last 2 hours")

            emit_dag_metric("AthenaEventCount2h", float(total_events), "churn_data_quality", unit="Count")

        except Exception as e:
            issues.append(f"Athena spot-check query failed: {e}")
            log.error(f"Athena query error: {e}")

        return {"issues": issues}

    athena_check = PythonOperator(
        task_id="check_athena_query",
        python_callable=check_athena_query,
        provide_context=True,
        execution_timeout=timedelta(minutes=10),
    )

    # ── Task 5: Aggregate results and alert ───────────────────────────────────
    def aggregate_and_alert(**context) -> None:
        """
        Collect issues from all checks, emit a summary metric,
        and raise an exception (which triggers on_failure_callback → Slack)
        if any critical issues were found.
        """
        all_issues = []
        for task_id in [
            "check_kinesis_health",
            "check_s3_freshness",
            "check_glue_crawler",
            "check_athena_query",
        ]:
            result = context["task_instance"].xcom_pull(task_ids=task_id) or {}
            task_issues = result.get("issues", [])
            if task_issues:
                all_issues.extend(task_issues)
                log.warning(f"[{task_id}] {len(task_issues)} issue(s) found")

        if all_issues:
            emit_dag_metric("DataQualityIssues", float(len(all_issues)), "churn_data_quality", unit="Count")
            summary = "\n".join(f"  • {i}" for i in all_issues)
            raise RuntimeError(
                f"Data quality check found {len(all_issues)} issue(s):\n{summary}"
            )
        else:
            emit_dag_metric("DataQualityIssues", 0.0, "churn_data_quality", unit="Count")
            log.info("All data quality checks passed")

    aggregate_task = PythonOperator(
        task_id="aggregate_and_alert",
        python_callable=aggregate_and_alert,
        provide_context=True,
        # Run even if some checks failed (we want to collect all issues)
        trigger_rule="all_done",
    )

    # ── Task ordering ──────────────────────────────────────────────────────────
    # All 4 checks run in parallel, then aggregate
    [kinesis_check, s3_freshness_check, glue_check, athena_check] >> aggregate_task
