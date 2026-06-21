# Runbook: Feature or Training Pipeline Failure

**Severity:** WARNING (data quality failure) / CRITICAL (training blocked >24h)  
**Alert name:** `AirflowSchedulerNotRunning`, `FeaturePipelineSLABreach`, Slack on-failure callback  
**Slack channel:** #ml-platform (warning), #alerts-critical (scheduler down)  

---

## Symptom

One or more of:
- Slack message: "DAG churn_feature_pipeline — Task `wait_raw_to_curated` FAILED"
- No model training triggered after 04:00 UTC
- `FeaturePipelineSLABreach` alert fires (DAG took >2 hours)
- `AirflowSchedulerNotRunning` alert fires (scheduler heartbeat missing)

---

## Step 1 — Locate the failure (3 min)

```bash
# Check Airflow scheduler is running
kubectl get pods -n airflow -l component=scheduler

# Access Airflow UI (port-forward if no ingress)
kubectl port-forward -n airflow svc/airflow-webserver 8080:8080
# Open: http://localhost:8080  (credentials from Secrets Manager)

# Or check via CLI — list recent DAG runs
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags list-runs -d churn_feature_pipeline --limit 5
```

In the Airflow UI: click the failed DAG run → click the red task → "Log" tab to see the full task log.

---

## Step 2 — Diagnose by failing task

### `wait_for_raw_data` failed (S3KeySensor timeout)
**Meaning:** No new data arrived in `s3://raw-bucket/events/YYYY/MM/DD/` by 06:00 UTC (4h sensor timeout).

```bash
# Check if Kinesis Firehose has been delivering
aws s3 ls s3://${RAW_BUCKET}/events/$(date -d 'yesterday' +%Y/%m/%d)/ | tail -5

# Check Kinesis stream health
aws cloudwatch get-metric-statistics \
  --namespace AWS/Kinesis \
  --metric-name IncomingRecords \
  --dimensions Name=StreamName,Value=churn-events \
  --start-time $(date -d '3 hours ago' -u +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 --statistics Sum
```

**Fix:** If Kinesis shows 0 IncomingRecords, the event producer is down (application-side issue, not platform). Notify the application team. The Airflow DAG will retry the sensor on the next scheduled run. You can also manually trigger with yesterday's partition using `logical_date` override.

### `wait_raw_to_curated` or `wait_feature_eng` failed (Glue job failure)
```bash
# Get the Glue job run ID from the Airflow task log (look for "run_id=")
# Then check the Glue job directly
aws glue get-job-run \
  --job-name churn-platform-dev-raw-to-curated \
  --run-id jr_<id_from_log>

# Check Glue job CloudWatch logs
aws logs tail /aws/glue/churn-platform-dev --since 2h
```

Common Glue failures:
- **`RESOURCE_NOT_FOUND`** — Glue catalog table missing. Run the Glue Crawler manually:
  `aws glue start-crawler --name churn-platform-dev-raw-crawler`
- **`INSUFFICIENT_CAPACITY`** — Glue DPU capacity unavailable. Retry the task in Airflow UI.
- **Schema mismatch** — New field added to raw data not handled in ETL script.
  Check `data_pipeline/` for the ETL code and update the schema mapping.
- **Job bookmark issue** — Bookmark thinks data was already processed. Reset:
  `aws glue reset-job-bookmark --job-name churn-platform-dev-raw-to-curated`

### `validate_features` failed (data validation gate)
The ShortCircuitOperator stops the pipeline here — no training triggered.
```bash
# Read the validation result from the failed task
kubectl exec -n airflow <failed-task-pod> -- cat /tmp/validation_result.json 2>/dev/null ||
  # Or read from S3 (the validation script writes here too)
  aws s3 cp s3://${PROCESSED_BUCKET}/validation/$(date +%Y-%m-%d)/result.json -
```

The validation checks and their fixes:
| Check | Likely cause | Fix |
|-------|-------------|-----|
| `schema` | New/removed column in raw data | Update ETL schema, rerun |
| `freshness` | Data >48h old | Check Kinesis/Firehose, manual backfill |
| `volume` | <1000 rows per class | Low traffic day; lower threshold or skip |
| `churn_rate` | Outside 3–30% range | Feature engineering bug or class explosion |
| `null_rate` | >50% nulls in a feature | Upstream field removed from application |
| `zero_variance` | Feature is constant | Remove the feature, retrain |
| `duplicates` | >1% duplicate rows | Firehose delivered duplicate records |

To **bypass validation** for a manual backfill run (use with care):
```bash
# Trigger the training pipeline directly, skipping the feature pipeline
airflow dags trigger churn_training_pipeline \
  --conf '{"execution_date": "2024-01-15", "skip_validation": true}'
```

### Training task failed (`submit_xgb_job` or `submit_pytorch_job`)
```bash
# The Airflow task log will contain the SageMaker job name
# Check SageMaker job status
aws sagemaker describe-training-job --training-job-name churn-xgboost-<sha>-<ts>

# Common statuses:
#   WaitingForCapacity — Spot capacity unavailable (wait up to 24h or switch to on-demand)
#   Failed — check FailureReason field
```

For Spot capacity issues:
```bash
# Re-submit as on-demand (edit submit_training_job.py temporarily or use HPO fallback)
SPOT_ENABLED=false python ml/training/submit_training_job.py \
  --model-type xgboost \
  --execution-date 2024-01-15
```

---

## Step 3 — Manual pipeline recovery

```bash
# Option A: Rerun just the failed task (Airflow UI → "Clear" the failed task)
# This is the preferred approach — Airflow reruns from the failed task.

# Option B: Trigger a full pipeline run for a specific date
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags trigger churn_feature_pipeline \
    --conf '{"execution_date": "2024-01-15"}'

# Option C: If the feature data is already in S3 (Glue ran but DAG was cleared),
# trigger training directly to avoid re-running Glue
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags trigger churn_training_pipeline \
    --conf '{"feature_execution_date": "2024-01-15"}'
```

---

## Step 4 — Scheduler is down (`AirflowSchedulerNotRunning`)

```bash
# Check scheduler pod
kubectl get pods -n airflow -l component=scheduler
kubectl describe pod -n airflow -l component=scheduler | tail -30

# Restart the scheduler
kubectl rollout restart deployment/airflow-scheduler -n airflow
kubectl rollout status deployment/airflow-scheduler -n airflow --timeout=5m

# Verify heartbeat (should update every 5s)
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow jobs check --job-type SchedulerJob --limit 1
```

If Aurora is the cause (scheduler can't connect to metadata DB):
```bash
# Check Aurora cluster status
aws rds describe-db-clusters \
  --query 'DBClusters[?contains(DBClusterIdentifier, `churn-platform`)].{Id:DBClusterIdentifier,Status:Status,Capacity:ServerlessV2ScalingConfiguration}'

# Aurora Serverless v2 may have scaled to 0 (dev only) — first connection wakes it.
# The scheduler will reconnect automatically once Aurora resumes.
```

---

## Prevention

| Risk | Control |
|------|---------|
| Raw data not arriving | `churn_data_quality` DAG checks Kinesis health hourly |
| Glue job silently skipping data | Job bookmark reset procedure in this runbook |
| Validation too strict for backfills | `skip_validation` DAG param for manual runs |
| Spot capacity blocking training >24h | `MaxWaitTimeInSeconds=86400` hard limit; falls back to failure |
| Scheduler down undetected | `AirflowSchedulerNotRunning` alert fires after 5 min |
