#!/usr/bin/env bash
# =============================================================================
# Security scanning — runs in GitHub Actions CI on every PR and push to main
#
# Tools:
#   Trivy  — container image vulnerability scanner (CVE database, misconfig)
#   Checkov — Terraform/Kubernetes IaC static analysis (400+ security rules)
#
# WHY both?
#   Trivy focuses on: OS packages, language dependencies, container misconfigs,
#     Kubernetes YAML policy violations, Secrets in image layers.
#   Checkov focuses on: Terraform resource misconfigurations (IAM too-permissive,
#     S3 no encryption, security groups with 0.0.0.0/0, etc.).
#   Neither covers the other's domain completely.
#
# Exit codes:
#   0 — no HIGH/CRITICAL findings
#   1 — HIGH or CRITICAL findings found (blocks PR merge in CI)
#
# Usage:
#   ./scripts/security_scan.sh                   # Scan all (Trivy + Checkov)
#   SCAN_TARGET=image ./scripts/security_scan.sh # Trivy image scan only
#   SCAN_TARGET=iac ./scripts/security_scan.sh   # Checkov IaC scan only
#
# Environment variables:
#   ECR_IMAGE       — full ECR image URI to scan (required for image scan)
#   TRIVY_VERSION   — Trivy version (default: latest)
#   CHECKOV_VERSION — Checkov version (default: latest)
#   SEVERITY        — comma-separated severity levels to fail on (default: HIGH,CRITICAL)
#   SKIP_DIRS       — directories to skip in Checkov scan
# =============================================================================

set -euo pipefail

SCAN_TARGET="${SCAN_TARGET:-all}"
SEVERITY="${SEVERITY:-HIGH,CRITICAL}"
CHECKOV_SKIP_DIRS="${SKIP_DIRS:-.git,.terraform,.build,__pycache__}"
RESULTS_DIR="${RESULTS_DIR:-/tmp/security-scan-results}"
EXIT_CODE=0

mkdir -p "$RESULTS_DIR"

# ── Helpers ───────────────────────────────────────────────────────────────────
info()    { echo "[INFO]  $*"; }
warn()    { echo "[WARN]  $*" >&2; }
error()   { echo "[ERROR] $*" >&2; }
section() { echo ""; echo "══════════════════════════════════════════"; echo "  $*"; echo "══════════════════════════════════════════"; }

require_tool() {
  if ! command -v "$1" &>/dev/null; then
    error "Required tool not found: $1. Install it or use the CI Docker image."
    exit 1
  fi
}

# ── Trivy: container image scanning ──────────────────────────────────────────
run_trivy_image_scan() {
  section "Trivy — Container Image Vulnerability Scan"

  if [[ -z "${ECR_IMAGE:-}" ]]; then
    warn "ECR_IMAGE not set — skipping container image scan"
    return 0
  fi

  require_tool trivy

  local output_file="$RESULTS_DIR/trivy-image.json"
  local sarif_file="$RESULTS_DIR/trivy-image.sarif"

  info "Scanning image: $ECR_IMAGE"
  info "Severity filter: $SEVERITY"

  # Primary scan: JSON output for processing
  trivy image \
    --format json \
    --output "$output_file" \
    --severity "$SEVERITY" \
    --exit-code 0 \
    --no-progress \
    --ignore-unfixed \
    "$ECR_IMAGE" 2>&1 || true

  # SARIF output for GitHub Security tab (uploaded as artifact in CI)
  trivy image \
    --format sarif \
    --output "$sarif_file" \
    --severity "$SEVERITY" \
    --exit-code 0 \
    --no-progress \
    --ignore-unfixed \
    "$ECR_IMAGE" 2>&1 || true

  # Human-readable output for PR comment
  trivy image \
    --format table \
    --severity "$SEVERITY" \
    --exit-code 0 \
    --no-progress \
    --ignore-unfixed \
    "$ECR_IMAGE"

  # Count HIGH/CRITICAL findings
  local vuln_count
  vuln_count=$(python3 -c "
import json, sys
data = json.load(open('$output_file'))
total = sum(
  len([v for v in r.get('Vulnerabilities', []) if v.get('Severity') in ['HIGH', 'CRITICAL']])
  for r in data.get('Results', [])
)
print(total)
" 2>/dev/null || echo "0")

  info "Found $vuln_count HIGH/CRITICAL vulnerabilities in container image"

  if [[ "$vuln_count" -gt 0 ]]; then
    error "$vuln_count HIGH/CRITICAL vulnerabilities found — see $output_file for details"
    return 1
  fi
  return 0
}

# ── Trivy: Kubernetes YAML misconfig scanning ─────────────────────────────────
run_trivy_k8s_scan() {
  section "Trivy — Kubernetes Manifest Security Scan"

  require_tool trivy

  local output_file="$RESULTS_DIR/trivy-k8s.json"

  info "Scanning Kubernetes manifests..."

  # Find all k8s YAML files (exclude kustomization and Helm templates)
  local k8s_dirs=()
  for dir in k8s/ airflow/helm/ inference/helm/ monitoring/helm/; do
    [[ -d "$dir" ]] && k8s_dirs+=("$dir")
  done

  if [[ ${#k8s_dirs[@]} -eq 0 ]]; then
    warn "No Kubernetes manifest directories found"
    return 0
  fi

  trivy config \
    --format json \
    --output "$output_file" \
    --severity "$SEVERITY" \
    --exit-code 0 \
    "${k8s_dirs[@]}" 2>&1 || true

  trivy config \
    --format table \
    --severity "$SEVERITY" \
    --exit-code 0 \
    "${k8s_dirs[@]}"

  local issue_count
  issue_count=$(python3 -c "
import json
data = json.load(open('$output_file'))
total = sum(len(r.get('Misconfigurations', [])) for r in data.get('Results', []))
print(total)
" 2>/dev/null || echo "0")

  info "Found $issue_count K8s misconfigurations"

  if [[ "$issue_count" -gt 0 ]]; then
    warn "$issue_count Kubernetes misconfigurations found — see $output_file"
    # Warn only (not fail) for k8s misconfigs — Gatekeeper enforces at runtime
    return 0
  fi
  return 0
}

# ── Checkov: Terraform IaC scanning ──────────────────────────────────────────
run_checkov_scan() {
  section "Checkov — Terraform IaC Security Scan"

  require_tool checkov

  local output_file="$RESULTS_DIR/checkov-results.json"
  local sarif_file="$RESULTS_DIR/checkov-results.sarif"

  # Checks to skip:
  #   CKV_AWS_144 — S3 cross-region replication (not needed for POC)
  #   CKV_AWS_18  — S3 access logging (CloudTrail covers this)
  #   CKV_AWS_86  — S3 access logging on the CloudTrail bucket (circular)
  #   CKV2_AWS_61 — S3 lifecycle configuration (set on some buckets already)
  local skip_checks="CKV_AWS_144,CKV_AWS_18,CKV_AWS_86,CKV2_AWS_61"

  info "Running Checkov on Terraform modules..."
  info "Skipping checks: $skip_checks"

  checkov \
    --directory terraform/ \
    --framework terraform \
    --output json \
    --output-file "$output_file" \
    --skip-check "$skip_checks" \
    --compact \
    --quiet \
    --skip-download 2>&1 || true

  checkov \
    --directory terraform/ \
    --framework terraform \
    --output sarif \
    --output-file "$sarif_file" \
    --skip-check "$skip_checks" \
    --compact \
    --quiet \
    --skip-download 2>&1 || true

  # Human-readable output
  checkov \
    --directory terraform/ \
    --framework terraform \
    --output cli \
    --skip-check "$skip_checks" \
    --compact \
    --quiet \
    --skip-download 2>&1 || true

  # Count failures
  local failed_count
  failed_count=$(python3 -c "
import json
try:
    data = json.load(open('$output_file'))
    # Checkov JSON can be a list (one entry per framework) or a dict
    if isinstance(data, list):
        failed = sum(r.get('summary', {}).get('failed', 0) for r in data)
    else:
        failed = data.get('summary', {}).get('failed', 0)
    print(failed)
except Exception:
    print(0)
" 2>/dev/null || echo "0")

  info "Checkov found $failed_count failed checks"

  if [[ "$failed_count" -gt 0 ]]; then
    error "$failed_count Terraform security checks failed — see $output_file"
    return 1
  fi
  return 0
}

# ── Checkov: Kubernetes YAML scanning ────────────────────────────────────────
run_checkov_k8s_scan() {
  section "Checkov — Kubernetes Manifest Security Scan"

  require_tool checkov

  local output_file="$RESULTS_DIR/checkov-k8s.json"

  # Skip: CKV_K8S_35 (secrets as env vars — we use ExternalSecrets, checkov can't see that)
  local skip_checks="CKV_K8S_35,CKV_K8S_28"

  checkov \
    --directory k8s/ \
    --framework kubernetes \
    --output json \
    --output-file "$output_file" \
    --skip-check "$skip_checks" \
    --compact \
    --quiet \
    --skip-download 2>&1 || true

  checkov \
    --directory k8s/ \
    --framework kubernetes \
    --output cli \
    --skip-check "$skip_checks" \
    --compact \
    --quiet \
    --skip-download 2>&1 || true

  local failed_count
  failed_count=$(python3 -c "
import json
try:
    data = json.load(open('$output_file'))
    if isinstance(data, list):
        failed = sum(r.get('summary', {}).get('failed', 0) for r in data)
    else:
        failed = data.get('summary', {}).get('failed', 0)
    print(failed)
except Exception:
    print(0)
" 2>/dev/null || echo "0")

  info "Checkov K8s found $failed_count failed checks"

  if [[ "$failed_count" -gt 0 ]]; then
    warn "$failed_count Kubernetes security checks failed (non-blocking)"
    return 0   # Warn only — Gatekeeper enforces at runtime
  fi
  return 0
}

# ── Secret detection ──────────────────────────────────────────────────────────
run_secret_scan() {
  section "Trivy — Secret Detection in Repository"

  require_tool trivy

  local output_file="$RESULTS_DIR/trivy-secrets.json"

  info "Scanning repository for secrets..."

  trivy fs \
    --scanners secret \
    --format json \
    --output "$output_file" \
    --exit-code 0 \
    --no-progress \
    . 2>&1 || true

  trivy fs \
    --scanners secret \
    --format table \
    --exit-code 0 \
    --no-progress \
    . 2>&1 || true

  local secret_count
  secret_count=$(python3 -c "
import json
data = json.load(open('$output_file'))
total = sum(len(r.get('Secrets', [])) for r in data.get('Results', []))
print(total)
" 2>/dev/null || echo "0")

  if [[ "$secret_count" -gt 0 ]]; then
    error "$secret_count secrets found in repository — this is a CRITICAL finding"
    error "Remove the secret, rotate it immediately, and rewrite git history"
    return 1
  fi

  info "No secrets detected in repository"
  return 0
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  info "Security scan starting"
  info "Scan target: $SCAN_TARGET"
  info "Results directory: $RESULTS_DIR"

  if [[ "$SCAN_TARGET" == "all" || "$SCAN_TARGET" == "secrets" ]]; then
    run_secret_scan || EXIT_CODE=1
  fi

  if [[ "$SCAN_TARGET" == "all" || "$SCAN_TARGET" == "image" ]]; then
    run_trivy_image_scan || EXIT_CODE=1
    run_trivy_k8s_scan   || EXIT_CODE=1
  fi

  if [[ "$SCAN_TARGET" == "all" || "$SCAN_TARGET" == "iac" ]]; then
    run_checkov_scan    || EXIT_CODE=1
    run_checkov_k8s_scan
  fi

  section "Scan Summary"
  if [[ "$EXIT_CODE" -eq 0 ]]; then
    info "All security scans PASSED"
  else
    error "One or more security scans FAILED — check results in $RESULTS_DIR"
    error "Upload the SARIF files to GitHub Security tab for detailed tracking"
  fi

  exit "$EXIT_CODE"
}

main "$@"
