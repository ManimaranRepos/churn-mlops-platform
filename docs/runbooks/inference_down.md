# Runbook: Inference Service Unavailable

**Severity:** CRITICAL  
**Alert name:** `InferenceServiceDown` or `InferenceHighErrorRate`  
**SLA:** Restore to <5% error rate within 30 minutes  
**Slack channel:** #alerts-critical  

---

## Symptom

One or more of:
- Alert `InferenceServiceDown` fires (no metrics received for 5 min)
- Alert `InferenceHighErrorRate` fires (error rate >5% for 5 min)
- CRM team reports predictions returning 500 errors
- API Gateway 5xx alarm fires in CloudWatch

---

## Step 1 — Confirm the scope (2 min)

```bash
# Check pod status in the inference namespace
kubectl get pods -n inference -o wide

# Check recent events (OOMKill, ImagePullBackOff, CrashLoopBackOff)
kubectl get events -n inference --sort-by='.lastTimestamp' | tail -20

# Check HPA state (is it trying to scale?)
kubectl get hpa -n inference
```

Expected healthy output: all pods `Running 1/1`, no recent Warning events.

---

## Step 2 — Identify the failure mode

### Mode A: All pods in CrashLoopBackOff
```bash
# Get logs from the crashed pod (previous container)
kubectl logs -n inference -l app=churn-inference --previous --tail=100
```

Common causes and fixes:
- **`RuntimeError: Model not loaded`** → MLflow is unreachable. Check MLflow pod:
  `kubectl get pods -n mlflow` and `kubectl logs -n mlflow -l app=mlflow --tail=50`
- **`redis.exceptions.ConnectionError`** → Redis URL wrong or ElastiCache unreachable.
  The server still starts (cache degrades gracefully) — this alone should NOT crash it.
  If crashing, it's a startup probe failure during model download, not Redis.
- **`OOMKilled`** → Model larger than memory limit. Increase `resources.limits.memory`
  in `inference/helm/values.yaml` and roll the deployment.
- **`ImagePullBackOff`** → ECR image tag doesn't exist or ECR permissions broken.
  Check: `kubectl describe pod -n inference <pod-name> | grep -A5 Events`

### Mode B: Pods running but returning 503
```bash
# Check readiness probe — is the model actually loaded?
kubectl exec -n inference deploy/churn-inference -- curl -s http://localhost:8080/health/ready | python3 -m json.tool
```

If `"status": "not_ready"`:
- `model: {"status": "not_loaded"}` → model download in progress or failed (check logs)
- `redis: {"status": "unhealthy"}` → ElastiCache issue (predictions still work — check if 503s are real)

### Mode C: Pods healthy but ALB not routing traffic
```bash
# Check ALB target group health in the AWS console or via CLI
aws elbv2 describe-target-health \
  --target-group-arn $(aws elbv2 describe-target-groups \
    --query 'TargetGroups[?contains(TargetGroupName, `inference`)].TargetGroupArn' \
    --output text)
```

If targets show `unhealthy`: readiness probe is failing. See Mode B above.

### Mode D: High error rate but pods healthy
```bash
# Sample recent errors from CloudWatch Logs Insights
aws logs start-query \
  --log-group-name /aws/eks/churn-platform-dev/inference \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string 'fields @timestamp, @message | filter status_code >= 400 | sort @timestamp desc | limit 20'
```

Check for: missing features in requests (422), malformed JSON (400), upstream dependency errors (502/504).

---

## Step 3 — Immediate mitigation

### Option A: Roll back to previous working image (fastest, <5 min)
```bash
# Find the previous image tag from ArgoCD history
argocd app history churn-inference

# Roll back to the last healthy revision
argocd app rollback churn-inference <revision-number>
```

### Option B: Force pod restart (if image is correct but model load failed)
```bash
kubectl rollout restart deployment/churn-inference -n inference
kubectl rollout status deployment/churn-inference -n inference --timeout=5m
```

### Option C: Emergency rollback to previous MLflow Production model
If the model itself is causing errors (wrong feature schema, corrupted artifact):
```bash
# Run the rollback script (restores traffic + MLflow registry state)
python ml/deployment/rollback.py \
  --environment dev \
  --model-name churn-prediction \
  --reason "inference_errors_post_promotion"
```

This script:
1. Restores ALB to 100% stable target group (immediate traffic shift)
2. Reads deployment history from S3 to find previous Production version
3. Archives the failed version in MLflow
4. Restores the previous Production version

---

## Step 4 — Verify recovery

```bash
# Confirm error rate is dropping
kubectl logs -n inference -l app=churn-inference -f | grep -E '"status_code":[45]'

# Hit the health endpoint directly
kubectl exec -n inference deploy/churn-inference -- curl -s http://localhost:8080/health/ready

# Check the Grafana dashboard
# https://grafana.internal/d/churn-platform-overview
# Panel: "Error Rate (5m)" should return to green
```

The `InferenceServiceDown` alert resolves automatically after 5 minutes without new data.
The `InferenceHighErrorRate` alert resolves after error rate drops below 5% for 5 minutes.

---

## Step 5 — Post-incident

1. Write a brief incident summary in the #ml-platform Slack channel
2. Open a GitHub issue with: timeline, root cause, affected requests count, fix applied
3. If root cause was a bad model: check `ml/evaluation/evaluate_model.py` quality gates —
   did the gate fail and get bypassed, or did the gates pass and the model regressed in prod?
4. If root cause was an infrastructure change: add a Config rule or Gatekeeper constraint to prevent recurrence

---

## Prevention

| Risk | Control |
|------|---------|
| Bad model passes quality gates | Tighten thresholds in `ml/evaluation/evaluate_model.py` |
| Image corruption | Trivy scan in CI blocks HIGH/CRITICAL CVEs |
| Memory exhaustion | OOMKilled alert + Gatekeeper RequireResourceLimits |
| Model download fails at startup | 3-minute startup probe (18 × 10s) before liveness kicks in |
| All pods down simultaneously | PDB `minAvailable: 1` prevents full drain during node maintenance |
