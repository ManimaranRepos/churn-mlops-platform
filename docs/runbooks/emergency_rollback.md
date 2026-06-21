# Runbook: Emergency Model Rollback

**Severity:** CRITICAL  
**When to use:** New model in production is causing errors, wrong predictions, or business impact  
**Time to restore:** <5 minutes for traffic; <15 minutes for full rollback  
**Slack channel:** #alerts-critical  

---

## Decision tree

```
New model promoted to Production
         │
         ▼
Error rate > 5% for >5 min?  ──YES──► Rollback NOW (Step 1A)
         │
         NO
         │
P99 latency > 500ms for >10 min?  ──YES──► Rollback NOW (Step 1A)
         │
         NO
         │
Prediction rate anomaly (>50% churn)?  ──YES──► Investigate first (Step 1B)
         │
         NO
         │
Business report of wrong predictions?  ──YES──► Investigate, then decide
```

---

## Step 1A — Immediate traffic rollback (<2 min)

The fastest action is restoring ALB traffic to the previous stable target group.
This does NOT require the rollback script — it's a single AWS CLI call.

```bash
# Find the stable target group ARN
STABLE_TG_ARN=$(aws elbv2 describe-target-groups \
  --query 'TargetGroups[?contains(TargetGroupName, `stable`)].TargetGroupArn' \
  --output text)

# Find the ALB listener ARN
LISTENER_ARN=$(aws elbv2 describe-listeners \
  --query 'Listeners[?contains(LoadBalancerArn, `churn-platform`)].ListenerArn' \
  --output text | head -1)

# Restore 100% traffic to stable
aws elbv2 modify-listener \
  --listener-arn ${LISTENER_ARN} \
  --default-actions Type=forward,TargetGroupArn=${STABLE_TG_ARN}

echo "Traffic restored to stable target group"
```

**Verify:** Error rate in Grafana should drop within 30 seconds.

---

## Step 1B — Investigate before rollback (if no hard errors)

```bash
# Check what the new model is predicting
kubectl exec -n inference deploy/churn-inference -- \
  curl -s http://localhost:8080/health/ready | python3 -m json.tool

# Sample predictions from a known-good customer
# (replace with a real customer_id from your test set)
curl -X POST https://<api-gateway-url>/predict \
  -H "X-Api-Key: <ml_pipeline_key>" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "TEST_001", "features": {...}}'

# Check if the model version matches what was promoted
# Expected: the version you just promoted
```

If predictions look correct but business team is alarmed, this may be a real signal
(the model correctly identified more churners) — confirm before rolling back.

---

## Step 2 — Full rollback with script

After stabilising traffic (Step 1A), run the full rollback to restore MLflow state:

```bash
python ml/deployment/rollback.py \
  --environment prod \
  --model-name churn-prediction \
  --reason "high_error_rate_post_promotion"
```

This script:
1. Confirms ALB is already on stable TG (or fixes it if not)
2. Reads `s3://artifacts/deployments/prod/churn-prediction/latest.json` for previous version
3. Archives the failed model version in MLflow (`stage=Archived`)
4. Restores the previous Production version in MLflow
5. Writes incident record to `s3://artifacts/incidents/prod/churn-prediction/<ts>_rollback.json`

Expected output:
```
[INFO] ALB traffic already on stable TG — skipping traffic shift
[INFO] Previous Production version: 11
[INFO] Archiving failed version: 12
[INFO] Restored version 11 to Production stage
[INFO] Incident record written to s3://...
[INFO] Rollback complete in 47s
```

---

## Step 3 — Verify rollback

```bash
# Confirm MLflow shows version 11 (or previous) as Production
MLFLOW_TRACKING_URI=<uri> python3 -c "
import mlflow
c = mlflow.tracking.MlflowClient()
v = c.get_latest_versions('churn-prediction', stages=['Production'])[0]
print(f'Production model: version={v.version}, run_id={v.run_id}')
"

# Force inference pods to reload the model (they load at startup)
# The pods still have version 12 loaded in memory — rolling restart picks up version 11
kubectl rollout restart deployment/churn-inference -n inference
kubectl rollout status deployment/churn-inference -n inference --timeout=5m

# Confirm the reloaded pods are healthy
kubectl exec -n inference deploy/churn-inference -- \
  curl -s http://localhost:8080/health/ready | python3 -m json.tool
# Expected: "model": {"version": "11", "status": "ok"}

# Confirm error rate is back to baseline in Grafana
# Panel: "Error Rate (5m)" — should be <1%
```

---

## Step 4 — Post-incident investigation

```bash
# Read the incident record
aws s3 cp s3://${ARTIFACTS_BUCKET}/incidents/prod/churn-prediction/ . --recursive
cat *_rollback.json | python3 -m json.tool

# Compare version 11 vs version 12 evaluation metrics in MLflow
MLFLOW_TRACKING_URI=<uri> python3 -c "
import mlflow
c = mlflow.tracking.MlflowClient()
for ver in ['11', '12']:
    v = c.get_model_version('churn-prediction', ver)
    run = c.get_run(v.run_id)
    m = run.data.metrics
    print(f'v{ver}: AUC={m.get(\"test_auc\",\"?\"):.4f} F1={m.get(\"test_f1\",\"?\"):.4f} P99ms={m.get(\"p99_latency_ms\",\"?\"):.1f}')
"

# Check evaluation gate result for version 12
aws s3 cp s3://${ARTIFACTS_BUCKET}/evaluations/<run-id>/gate_result.json -
```

Common root causes:
- Quality gate thresholds too loose (passed a model that degraded in prod) → tighten thresholds in `ml/evaluation/evaluate_model.py`
- Training/serving skew (model trained on different features than it receives in prod) → compare feature schemas
- Canary monitoring window too short (30 min) → extend `monitor_duration_minutes` in `canary_deploy.py`
- Silent failure in preprocessing (null handling differs between training and inference) → add null rate assertions to `validate_data.py`

---

## Step 5 — Re-enable training for the fixed version

Once root cause is understood and fixed:

```bash
# The failed version is archived — it won't be auto-deployed again
# Trigger a fresh training run to produce a corrected model
kubectl exec -n airflow deploy/airflow-scheduler -- \
  airflow dags trigger churn_training_pipeline \
    --conf '{"trigger_reason": "post_rollback_retraining", "force_model": "xgboost"}'
```

---

## Rollback decision log

Document every rollback in the GitHub issue tracker with:
- Time of promotion
- Time of rollback
- Metrics at rollback time (error rate, P99, business impact)
- Root cause (one sentence)
- Fix applied
- Time to restore

This record feeds the quarterly model reliability review.
