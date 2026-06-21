# =============================================================================
# Inference server image
#
# WHY python:3.11-slim (not the ML-specific images)?
#   SageMaker base images are several GB — they include CUDA, cuDNN, and dozens
#   of pre-installed packages we don't need. Since this server runs on CPU
#   (XGBoost inference is fast on CPU; torch CPU wheel is <500MB vs >2GB GPU),
#   slim is preferred. Startup time matters for KubernetesExecutor pods and
#   for HPA scale-out latency.
#
# Build optimisations:
#   - torch CPU-only: --index-url for CPU wheel saves ~1.5GB vs the GPU default
#   - pip install in two layers: torch first (rarely changes), then the rest
#     (frequent changes). Docker layer cache skips torch re-download on code edits.
#   - Non-root user: UID 1000, GID 1000 (matches EKS node's default non-root UID)
# =============================================================================

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN addgroup --gid 1000 inference && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" inference

WORKDIR /app

# ── Install system deps ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── Install PyTorch CPU-only first (large layer, changes rarely) ──────────────
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
    torch==2.2.2+cpu

# ── Install remaining requirements ───────────────────────────────────────────
COPY requirements.txt .
RUN grep -v "^torch==" requirements.txt > /tmp/req_no_torch.txt && \
    pip install -r /tmp/req_no_torch.txt

# ── Copy application code ─────────────────────────────────────────────────────
COPY src/ ./src/

USER inference
WORKDIR /app/src

# 2 Gunicorn workers per pod (pod has 1 vCPU requested, 2 vCPU limit)
# k = 2*CPU+1 is the standard Gunicorn recommendation
# Each worker loads the model independently (~300–500MB per worker)
EXPOSE 8080
CMD ["gunicorn", "main:app", \
     "--workers", "2", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8080", \
     "--timeout", "30", \
     "--graceful-timeout", "15", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
