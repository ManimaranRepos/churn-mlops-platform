# Runbook: Model Drift Detected

**Severity:** WARNING (data drift) / WARNING (model quality drift)  
**Alert name:** `DataQualityCheckFailed`, `high_drift_score` CloudWatch alarm  
**Slack channel:** #ml-platform  

---

## Symptom

One or more of:
- Slack message: "Model Monitor: N violations" from the drift detector Lambda
- CloudWatch alarm `churn-platform-dev-model-drift-high` fires (DriftScore >0.3)
- Weekly `churn_model_monitoring` DAG completes with `retraining_triggered: true`
- Business reports unusually high or low churn predictions (>50% positive rate alarm)

---

## Understanding drift types

**Data Quality Drift** (feature distribution shift):
- The distribution of `customer_tenure_months` in live traffic no longer matches the training data
- Root causes: application changed how it computes a field, new customer segment, seasonal pattern
- Detection: SageMaker DataQuality Monitor runs every 6h, compares live data to `statistics.json` baseline

**Model Quality / Concept Drift** (accuracy degradation):
- The model's precision/recall on confirmed churn outcomes has dropped
- Root causes: customer behaviour changed, market event, promotional campaign
- Detection: SageMaker ModelQuality Monitor runs every 6h+30min using `merged_labels.csv`
- **Note:** Ground truth labels lag 30–90 days. Concept drift detection is inherently delayed.

---

## Step 1 — Read the violation report (5 min)

```bash
# Find the latest drift summary from S3 (written by drift_detector.py every run)
aws s3 ls s3://${ARTIFACTS_BUCKET}/model-monitor/drift-summaries/dev/$(date +%Y/%m/%d)/ \
  | sort | tail -3

# Download and read the latest
aws s3 cp s3://${ARTIFACTS_BUCKET}/model-monitor/drift-summaries/dev/$(date +%Y/%m/%d)/<latest>.json - \
  | python3 -m json.tool
```

Key fields to read:
- `violation_count` — total number of violated constraints
- `drift_score` — 0.0–1.0 severity score (≥0.3 triggers retraining)
- `critical_features` — high-importance features that violated constraints
- `retraining_triggered` — whether auto-retraining was already started

```bash
# Also read the raw violation report from SageMaker
aws sagemaker list-monitoring-executions \
  --monitoring-schedule-name churn-platform-dev-data-quality-schedule \
  --sort-by CreationTime --sort-order Descending \
  --max-results 3

# Get the violation details from the latest execution's S3 report
aws s3 cp s3://${ARTIFACTS_BUCKET}/model-monitor/reports/data-quality/<execution-id>/violations.json - \
  | python3 -c "import json,sys; v=json.load(sys.stdin); [print(x['feature_name'], x['constraint_check_type']) for x in v.get('violations',[])]"
```

---

## Step 2 — Classify the drift

### Scenario A: Minor drift (score 0.1–0.3, no critical features)
- **Action:** Monitor for 1–2 more monitoring cycles (12h). If stable, no action needed.
- Drift in low-importance features (e.g., `ui_theme_preference`) rarely affects churn prediction.
- Log the observation in the #ml-platform channel for awareness.

### Scenario B: Critical feature drift (score ≥0.3 or critical_features non-empty)
Auto-retraining was already triggered (check `retraining_triggered: true`).

```bash
# Confirm the retraining DAG run was created
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags list-runs -d churn_training_pipeline --limit 5

# The conf will show: "trigger_reason": "model_monitor_drift"
```

If auto-retraining was NOT triggered (Lambda failed):
```bash
# Manually trigger retraining
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags trigger churn_training_pipeline \
    --conf '{"trigger_reason": "manual_drift_response", "drift_score": 0.45}'
```

### Scenario C: Data pipeline issue (not real drift)
Some violations indicate upstream data problems rather than genuine distribution shift:
- `null_rate` violations for many features → upstream field removed from application
- `completeness` violations for a single feature → that specific field was renamed

```bash
# Check if raw data schema changed recently
aws glue get-table --database-name churn-platform-dev-raw --name customer_events \
  | python3 -c "import json,sys; t=json.load(sys.stdin); [print(c['Name'], c['Type']) for c in t['Table']['StorageDescriptor']['Columns']]"

# Compare to the baseline statistics to see which fields are new/removed
aws s3 cp s3://${ARTIFACTS_BUCKET}/model-monitor/baselines/<model-version>/statistics.json - \
  | python3 -c "import json,sys; s=json.load(sys.stdin); [print(f['name']) for f in s.get('features',[])]"
```

If it's a schema change: update the Glue ETL in `data_pipeline/`, re-run the feature pipeline, and recapture the baseline.

---

## Step 3 — Recapture baseline (after retraining)

After a new model is promoted to Production, the old baseline no longer applies.
The weekly `churn_model_monitoring` DAG does this automatically, but you can trigger manually:

```bash
# Get the current Production model version from MLflow
MLFLOW_TRACKING_URI=<uri> python3 -c "
import mlflow
client = mlflow.tracking.MlflowClient()
v = client.get_latest_versions('churn-prediction', stages=['Production'])[0]
print(f'Version: {v.version}')
"

# Capture new baseline
python ml/monitoring/baseline_capture.py \
  --model-version <version> \
  --execution-date $(date -d 'yesterday' +%Y-%m-%d)

# Verify baseline files were created
aws s3 ls s3://${ARTIFACTS_BUCKET}/model-monitor/baselines/<version>/
```

Expected: `constraints.json` and `statistics.json` both present.

---

## Step 4 — Update monitoring schedule with new baseline

After recapturing, Terraform must be re-applied with the new `model_version`:

```bash
cd terraform/
terraform workspace select dev
terraform apply \
  -target=module.model_monitor \
  -var="model_version=<new-version>" \
  -auto-approve
```

This updates the `DataQualityJobDefinition` to point to the new baseline files.

---

## Step 5 — Monitor post-retraining

After the new model is deployed:
```bash
# Watch for violations to decrease over the next 6h monitoring cycle
# Use the Grafana dashboard:
# Panel: "Model Drift Score" — should drop below 0.3

# Or watch CloudWatch directly
aws cloudwatch get-metric-statistics \
  --namespace churn-platform/ModelMonitor \
  --metric-name DriftScore \
  --dimensions Name=Environment,Value=dev Name=MonitoringType,Value=data_quality \
  --start-time $(date -d '1 day ago' -u +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 21600 --statistics Maximum
```

---

## Prevention

| Risk | Control |
|------|---------|
| Drift undetected for days | 6-hour monitoring schedule (not daily) |
| Wrong baseline after model promotion | Weekly DAG recaptures baseline automatically |
| Auto-retraining loop (drift → train → new drift → train) | Retraining only fires once per drift event (not re-evaluated until next monitoring cycle) |
| Concept drift missed due to label lag | Ground truth collector runs weekly; alerts when <100 matched records |
| Schema change causes false drift alarms | Data quality DAG runs hourly and alerts before Model Monitor fires |
