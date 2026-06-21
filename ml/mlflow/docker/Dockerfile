# =============================================================================
# MLflow Tracking Server
# Base: official Python slim image (smaller attack surface than full Python)
# =============================================================================
FROM python:3.11-slim AS base

# Install OS packages needed for psycopg2 compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Dependency layer (cached unless requirements change) ──────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application layer ─────────────────────────────────────────────────────────
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Run as non-root user (required by Pod Security Standard: restricted)
RUN useradd -m -u 1000 mlflow
USER mlflow

# MLflow server ports:
#   5000 = HTTP API + UI
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

ENTRYPOINT ["./entrypoint.sh"]
