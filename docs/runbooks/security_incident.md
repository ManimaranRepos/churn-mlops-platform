# Runbook: Security Incident Response

**Severity:** CRITICAL  
**Alert sources:** GuardDuty, Security Hub, CloudWatch metric filters, IAM Access Analyzer  
**Slack channel:** #alerts-critical  
**Escalation:** Platform lead + AWS account owner within 15 min of CRITICAL finding  

---

## Triage: What type of finding?

| Finding source | Typical severity | Response time |
|----------------|-----------------|---------------|
| GuardDuty: `UnauthorizedAccess:IAMUser/MaliciousIPCaller` | CRITICAL | Immediate |
| GuardDuty: `CryptoCurrency:EC2/BitcoinTool` | HIGH | 15 min |
| GuardDuty: `Execution:EKS/ExecInPod` | HIGH | 15 min |
| Security Hub: CIS control failure | MEDIUM | Next business day |
| CloudWatch: Root account usage | HIGH | 15 min |
| CloudWatch: IAM policy change (unexpected) | HIGH | 30 min |
| Access Analyzer: External access to S3 bucket | HIGH | 30 min |

---

## Step 1 — Get the finding details (3 min)

```bash
# From Security Hub — list active CRITICAL/HIGH findings
aws securityhub get-findings \
  --filters '{"SeverityLabel":[{"Value":"CRITICAL","Comparison":"EQUALS"},{"Value":"HIGH","Comparison":"EQUALS"}],"WorkflowStatus":[{"Value":"NEW","Comparison":"EQUALS"}]}' \
  --query 'Findings[].{Title:Title,Severity:Severity.Label,Resource:Resources[0].Id,Source:ProductArn}' \
  --output table

# From GuardDuty directly
DETECTOR_ID=$(aws guardduty list-detectors --query 'DetectorIds[0]' --output text)
aws guardduty list-findings \
  --detector-id ${DETECTOR_ID} \
  --finding-criteria '{"Criterion":{"severity":{"Gte":7},"service.archived":{"Eq":["false"]}}}' \
  --query 'FindingIds' --output text | xargs \
  aws guardduty get-findings --detector-id ${DETECTOR_ID} --finding-ids \
  --query 'Findings[].{Type:Type,Severity:Severity,Region:Region,Principal:Service.Action.AwsApiCallAction.RemoteIpDetails.IpAddressV4}' \
  --output table
```

---

## Step 2 — Respond by finding type

### `Execution:EKS/ExecInPod` — Suspicious kubectl exec
Someone `exec`'d into a pod from an unusual IP or at an unusual time.

```bash
# Find out which pod and what commands were run — CloudTrail captures kubectl exec
aws logs filter-log-events \
  --log-group-name /aws/cloudtrail/churn-platform-dev \
  --start-time $(date -d '2 hours ago' -s) \
  --filter-pattern '{ $.eventName = "exec" && $.requestURI = "*exec*" }' \
  --query 'events[].message' | python3 -m json.tool

# Identify the actor: userIdentity.arn tells you which IAM role did this
# If it was a GitHub Actions role executing a legitimate CI job → false positive
# If it was an unknown role or human IAM user in prod → escalate immediately

# If malicious: isolate the pod by adding a label that removes it from the ALB
kubectl label pod -n inference <pod-name> security-quarantine=true
kubectl patch service -n inference churn-inference \
  -p '{"spec":{"selector":{"app":"churn-inference","security-quarantine":null}}}'
```

### `UnauthorizedAccess:IAMUser/MaliciousIPCaller` — Creds used from known-malicious IP
```bash
# Find which key was used
aws guardduty get-findings \
  --detector-id ${DETECTOR_ID} \
  --finding-ids <finding-id> \
  --query 'Findings[0].Service.Action.AwsApiCallAction.{UserName:UserAgent,AccessKey:RemoteIpDetails}'

# Immediately disable the access key (even if it may be a false positive —
# disable first, investigate after, re-enable if needed)
aws iam update-access-key \
  --user-name <username> \
  --access-key-id <key-id> \
  --status Inactive

# Revoke all active sessions for this user
aws iam delete-user-policy --user-name <username> --policy-name <any_inline_policies>
# (or attach a Deny-all policy immediately)
aws iam put-user-policy --user-name <username> --policy-name EmergencyLockout \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"*","Resource":"*"}]}'
```

### CloudWatch alarm: `RootAccountUsage`
```bash
# Root login is almost never legitimate in a properly configured account.
# Find what was done
aws logs filter-log-events \
  --log-group-name /aws/cloudtrail/churn-platform-dev \
  --start-time $(date -d '1 hour ago' -s) \
  --filter-pattern '{ $.userIdentity.type = "Root" }' \
  --query 'events[].message' | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip().strip('\"').replace('\\\\n', '\n')
    try:
        event = json.loads(line)
        print(f\"Time: {event.get('eventTime')} | Action: {event.get('eventName')} | IP: {event.get('sourceIPAddress')}\")
    except: pass
"

# If this was not a legitimate break-glass operation:
# 1. Change root password immediately
# 2. Enable root MFA if not already enabled
# 3. Audit all actions taken during the root session (see CloudTrail above)
```

### IAM Access Analyzer: External access finding
```bash
# Get analyzer findings
aws accessanalyzer list-findings \
  --analyzer-arn $(aws accessanalyzer list-analyzers --query 'analyzers[0].arn' --output text) \
  --filter '{"status":{"eq":["ACTIVE"]}}' \
  --query 'findings[].{Resource:resource,Action:action,Principal:principal}' \
  --output table

# For each finding, check if the access is intentional (cross-account for partner)
# or accidental (Principal: *)

# To fix accidental public S3 bucket:
aws s3api put-public-access-block \
  --bucket <bucket-name> \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# Archive the finding once fixed (mark as resolved)
aws accessanalyzer update-findings \
  --analyzer-arn <analyzer-arn> \
  --ids <finding-id> \
  --status ARCHIVED
```

---

## Step 3 — Preserve evidence before remediating

**IMPORTANT:** Before deleting/modifying anything that might be compromised,
capture evidence for the post-incident review:

```bash
# Save CloudTrail events for the incident window
aws logs filter-log-events \
  --log-group-name /aws/cloudtrail/churn-platform-dev \
  --start-time <incident-start-epoch> \
  --end-time <incident-end-epoch> \
  --output json > /tmp/incident-cloudtrail-$(date +%s).json

# Save VPC flow logs for the relevant period
aws logs filter-log-events \
  --log-group-name /aws/vpc/flow-logs/churn-platform-dev \
  --start-time <incident-start-epoch> \
  --end-time <incident-end-epoch> \
  --output json > /tmp/incident-vpc-flows-$(date +%s).json

# Copy to a forensics S3 bucket (separate from the main buckets)
aws s3 cp /tmp/incident-cloudtrail-*.json s3://${ARTIFACTS_BUCKET}/incidents/forensics/
aws s3 cp /tmp/incident-vpc-flows-*.json  s3://${ARTIFACTS_BUCKET}/incidents/forensics/
```

---

## Step 4 — Notify and document

1. **Immediately:** Post in #alerts-critical with: finding type, affected resource, actions taken
2. **Within 1 hour:** Open a GitHub issue titled `[SECURITY] <finding-type> <date>`
3. **Within 24 hours:** Write a brief incident report (what happened, blast radius, fix, prevention)
4. **Archive Security Hub finding** once resolved:
   ```bash
   aws securityhub update-findings \
     --filters '{"Id":[{"Value":"<finding-id>","Comparison":"EQUALS"}]}' \
     --note '{"Text":"Remediated — see GitHub issue #<n>","UpdatedBy":"oncall"}' \
     --record-state ARCHIVED
   ```

---

## Prevention check after any security incident

- [ ] Was this finding something GuardDuty/Config/Security Hub could have caught earlier?
- [ ] Did the alert reach the right person within 5 minutes?
- [ ] Was the affected resource tagged correctly (ManagedBy=terraform)?
- [ ] Does the IAM role involved follow least-privilege?
- [ ] Add a new Config rule or CloudWatch metric filter if the finding type is not currently monitored
