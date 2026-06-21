#!/bin/bash
# =============================================================================
# MLflow Server Entrypoint
# Reads DB credentials from environment (injected by External Secrets Operator)
# and starts the tracking server.
# =============================================================================
set -euo pipefail

# Validate required environment variables
: "${MLFLOW_BACKEND_STORE_URI:?MLFLOW_BACKEND_STORE_URI must be set}"
: "${MLFLOW_DEFAULT_ARTIFACT_ROOT:?MLFLOW_DEFAULT_ARTIFACT_ROOT must be set}"

echo "Starting MLflow tracking server..."
echo "  Backend store: ${MLFLOW_BACKEND_STORE_URI%%@*}@[REDACTED]"
echo "  Artifact root: ${MLFLOW_DEFAULT_ARTIFACT_ROOT}"

# Run DB migrations before starting server
# Idempotent: safe to run on every startup
mlflow db upgrade "${MLFLOW_BACKEND_STORE_URI}"
echo "DB migrations complete."

# Start MLflow server
# --serve-artifacts: proxy artifact downloads through the server
#   (pods don't need S3 credentials — only the MLflow server does)
# --gunicorn-opts: production-grade WSGI with multiple workers
exec mlflow server \
    --backend-store-uri "${MLFLOW_BACKEND_STORE_URI}" \
    --default-artifact-root "${MLFLOW_DEFAULT_ARTIFACT_ROOT}" \
    --host 0.0.0.0 \
    --port 5000 \
    --serve-artifacts \
    --workers "${MLFLOW_WORKERS:-2}" \
    --gunicorn-opts "--timeout 120 --keep-alive 5 --log-level info"
