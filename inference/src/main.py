"""
FastAPI inference server for customer churn prediction.

Architecture:
  API Gateway HTTP API
    → ALB (weighted target groups: canary/stable)
      → EKS Service
        → FastAPI pods (this file)
          → Redis cache (check first)
          → MLflow model (if cache miss)

Endpoints:
  POST /predict          — single customer prediction
  POST /predict/batch    — up to 500 customers in one call
  DELETE /cache/{id}     — invalidate cached prediction for a customer
  GET  /health           — liveness (used by EKS readiness probe)
  GET  /health/ready     — readiness (checks model + Redis loaded)
  GET  /metrics          — Prometheus text exposition (scraped by Phase 8)

WHY async FastAPI (not Flask)?
  Redis calls are async (aioredis). Making them async means the worker thread
  is not blocked during Redis I/O — it can handle other requests. Under high
  concurrency, this is significantly more efficient than sync Flask + threading.
  A single Gunicorn worker with uvicorn can handle hundreds of concurrent
  requests while waiting on Redis, vs. one at a time with sync Flask.

WHY Gunicorn + Uvicorn (not just Uvicorn)?
  Uvicorn is single-process. Gunicorn forks multiple worker processes, so we
  utilize all CPU cores. Each Gunicorn worker runs a single Uvicorn event loop.
  For CPU-bound ML inference, multiple processes beat a single async loop.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from cache import cache_health, get_cached_prediction, invalidate_customer, set_cached_prediction
from predictor import LoadedModel, get_model, load_model, predict, predict_batch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "dev")
VERSION     = os.getenv("APP_VERSION", "unknown")

# ── Prometheus metrics (manual exposition — no prometheus_client dependency) ──
# We track counters and histograms in-process and expose them at /metrics.
# Phase 8 will configure Prometheus to scrape this endpoint.
_counters: dict[str, float] = {
    "requests_total":     0,
    "cache_hits_total":   0,
    "cache_misses_total": 0,
    "errors_total":       0,
    "batch_requests_total": 0,
}
_latency_buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
_latency_counts: dict[str, int] = {str(b): 0 for b in _latency_buckets}
_latency_counts["+Inf"] = 0
_latency_sum   = 0.0
_latency_total = 0


def _record_latency(seconds: float) -> None:
    global _latency_sum, _latency_total
    _latency_sum   += seconds
    _latency_total += 1
    for b in _latency_buckets:
        if seconds <= b:
            _latency_counts[str(b)] += 1
    _latency_counts["+Inf"] += 1


# ── Lifespan: load model at startup, release at shutdown ─────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Starting inference server (env={ENVIRONMENT}, version={VERSION})")
    load_model()
    log.info("Model loaded — server ready")
    yield
    log.info("Shutting down inference server")


app = FastAPI(
    title="Churn Prediction API",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs" if ENVIRONMENT != "prod" else None,   # disable Swagger in prod
    redoc_url=None,
)


# ── Request / Response schemas ────────────────────────────────────────────────
class PredictRequest(BaseModel):
    customer_id: str = Field(..., description="Unique customer identifier")
    features:    dict[str, Any] = Field(..., description="Feature values keyed by feature name")

    @model_validator(mode="after")
    def validate_features_not_empty(self) -> "PredictRequest":
        if not self.features:
            raise ValueError("features dict must not be empty")
        return self


class PredictResponse(BaseModel):
    customer_id:        str
    churn_probability:  float
    churn_prediction:   bool
    model_version:      str
    threshold:          float
    model_type:         str
    cached:             bool
    latency_ms:         float


class BatchPredictRequest(BaseModel):
    requests: list[PredictRequest] = Field(..., min_length=1, max_length=500)


class BatchPredictResponse(BaseModel):
    results:    list[PredictResponse]
    total:      int
    latency_ms: float


# ── Middleware: request timing + correlation ID ───────────────────────────────
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response: Response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{elapsed:.2f}"
    response.headers["X-Environment"]      = ENVIRONMENT
    return response


# ── Single prediction ─────────────────────────────────────────────────────────
@app.post("/predict", response_model=PredictResponse)
async def predict_endpoint(req: PredictRequest):
    """
    Predict churn probability for a single customer.

    Cache behaviour:
      - Cache key = hash(customer_id + features) — same features = same key
      - TTL = 300s (configurable via CACHE_TTL_SECONDS env var)
      - On Redis failure, falls through to model inference (graceful degradation)
    """
    _counters["requests_total"] += 1
    t0 = time.perf_counter()

    try:
        cached = await get_cached_prediction(req.customer_id, req.features)
        if cached:
            _counters["cache_hits_total"] += 1
            elapsed = (time.perf_counter() - t0) * 1000
            _record_latency(elapsed / 1000)
            return PredictResponse(
                customer_id       = req.customer_id,
                cached            = True,
                latency_ms        = round(elapsed, 2),
                **cached,
            )

        _counters["cache_misses_total"] += 1
        loaded = get_model()
        result = predict(req.features, loaded)

        await set_cached_prediction(req.customer_id, req.features, result)

        elapsed = (time.perf_counter() - t0) * 1000
        _record_latency(elapsed / 1000)

        return PredictResponse(
            customer_id = req.customer_id,
            cached      = False,
            latency_ms  = round(elapsed, 2),
            **result,
        )

    except ValueError as e:
        _counters["errors_total"] += 1
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        _counters["errors_total"] += 1
        log.exception(f"Prediction error for customer {req.customer_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal inference error")


# ── Batch prediction ──────────────────────────────────────────────────────────
@app.post("/predict/batch", response_model=BatchPredictResponse)
async def predict_batch_endpoint(req: BatchPredictRequest):
    """
    Predict churn for up to 500 customers in a single call.

    WHY batch endpoint?
      Batch callers (CRM export jobs, nightly scoring) would otherwise make
      hundreds of individual HTTP calls. A single batch call:
        - Reduces HTTP overhead (one TLS handshake, one TCP round-trip)
        - Lets the model score all rows in one vectorised call (10-50x faster)
        - Reduces Redis connection churn

    Cache: each customer is checked individually (different customers = different
    cache keys). The uncached subset is scored as a batch.
    """
    _counters["batch_requests_total"] += 1
    t0     = time.perf_counter()
    loaded = get_model()

    cached_results: dict[str, dict] = {}
    uncached_indices: list[int]     = []
    uncached_rows:    list[dict]    = []

    for i, r in enumerate(req.requests):
        cached = await get_cached_prediction(r.customer_id, r.features)
        if cached:
            _counters["cache_hits_total"] += 1
            cached_results[str(i)] = cached
        else:
            _counters["cache_misses_total"] += 1
            uncached_indices.append(i)
            uncached_rows.append(r.features)

    batch_preds: list[dict] = []
    if uncached_rows:
        batch_preds = predict_batch(uncached_rows, loaded)
        for j, idx in enumerate(uncached_indices):
            r = req.requests[idx]
            await set_cached_prediction(r.customer_id, r.features, batch_preds[j])

    results = []
    pred_idx = 0
    for i, r in enumerate(req.requests):
        if str(i) in cached_results:
            pred   = cached_results[str(i)]
            cached = True
        else:
            pred     = batch_preds[pred_idx]
            cached   = False
            pred_idx += 1

        results.append(PredictResponse(
            customer_id = r.customer_id,
            cached      = cached,
            latency_ms  = 0.0,   # per-item latency not meaningful in batch
            **pred,
        ))

    elapsed = (time.perf_counter() - t0) * 1000
    return BatchPredictResponse(
        results    = results,
        total      = len(results),
        latency_ms = round(elapsed, 2),
    )


# ── Cache invalidation ────────────────────────────────────────────────────────
@app.delete("/cache/{customer_id}")
async def invalidate_cache(customer_id: str):
    """
    Invalidate cached predictions for a customer.
    Called by the feature pipeline after a customer's features are refreshed,
    so the next prediction uses the updated features rather than the cached stale score.
    """
    deleted = await invalidate_customer(customer_id)
    return {"customer_id": customer_id, "entries_deleted": deleted}


# ── Health endpoints ──────────────────────────────────────────────────────────
@app.get("/health")
async def liveness():
    """
    Kubernetes liveness probe.
    Returns 200 if the process is alive. Does NOT check model/Redis.
    WHY separate liveness from readiness?
      If the model fails to load, liveness should still pass (process is alive)
      but readiness should fail (don't send traffic). Kubernetes will restart
      a pod that fails liveness — not what we want for a model load failure.
    """
    return {"status": "alive", "environment": ENVIRONMENT}


@app.get("/health/ready")
async def readiness():
    """
    Kubernetes readiness probe.
    Fails if model not loaded or Redis unreachable.
    EKS stops sending traffic to a pod that fails this check.
    """
    issues = []

    try:
        loaded = get_model()
        model_status = {"status": "ok", "version": loaded.model_version, "type": loaded.model_type}
    except RuntimeError as e:
        issues.append(str(e))
        model_status = {"status": "not_loaded"}

    redis_status = await cache_health()
    if redis_status["status"] != "healthy":
        issues.append(f"Redis unhealthy: {redis_status.get('error', 'unknown')}")

    if issues:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "issues": issues, "model": model_status, "redis": redis_status},
        )

    return {"status": "ready", "model": model_status, "redis": redis_status}


# ── Prometheus metrics ────────────────────────────────────────────────────────
@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    """
    Prometheus text format metrics.
    Phase 8 Prometheus will scrape this endpoint via ServiceMonitor.
    """
    lines = ["# HELP churn_api_requests_total Total prediction requests"]
    lines.append("# TYPE churn_api_requests_total counter")
    for name, val in _counters.items():
        lines.append(f'churn_api_{name}{{env="{ENVIRONMENT}"}} {val:.0f}')

    lines.append("# HELP churn_api_inference_latency_seconds Prediction latency histogram")
    lines.append("# TYPE churn_api_inference_latency_seconds histogram")
    for b, count in _latency_counts.items():
        lines.append(f'churn_api_inference_latency_seconds_bucket{{le="{b}",env="{ENVIRONMENT}"}} {count}')
    lines.append(f'churn_api_inference_latency_seconds_sum{{env="{ENVIRONMENT}"}} {_latency_sum:.6f}')
    lines.append(f'churn_api_inference_latency_seconds_count{{env="{ENVIRONMENT}"}} {_latency_total}')

    return "\n".join(lines) + "\n"
