"""
Model quality gate — runs after SageMaker training, before canary deploy.

WHY a separate evaluation script (not just reading MLflow metrics)?
  The training script logs metrics on the VALIDATION set (used for early stopping/HPO).
  This script evaluates on a HELD-OUT TEST SET that was never seen during training.
  Using val metrics as the gate would overfit our gating threshold to the val set.

Quality gates (aligned with .github/workflows/ml-pipeline.yml):
  - AUC   >= 0.82
  - Precision >= 0.75
  - Recall    >= 0.70
  - P99 latency < 200ms

If all gates pass, the script:
  1. Logs test metrics to the existing MLflow run
  2. Writes gate_result.json (read by GitHub Actions decide-deploy step)
  3. Exits 0

If any gate fails:
  1. Logs failure reason
  2. Writes gate_result.json with failed=true
  3. Exits 1 (blocks deployment)

Usage:
  python evaluate_model.py \
      --run-id <mlflow-run-id> \
      --test-data-s3 s3://bucket/processed/features/test/ \
      --model-type xgboost
"""

import argparse
import json
import logging
import os
import pickle
import sys
import time
from pathlib import Path
from typing import Optional

import boto3
import mlflow
import numpy as np
import pandas as pd
import xgboost as xgb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Quality gate thresholds (must match ml-pipeline.yml) ──────────────────────
QUALITY_GATES = {
    "auc":           0.82,
    "precision":     0.75,
    "recall":        0.70,
    "p99_latency_ms": 200.0,
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id",        required=True,  help="MLflow run ID from training job")
    parser.add_argument("--test-data-s3",  required=True,  help="S3 path to test parquet files")
    parser.add_argument("--model-type",    required=True,  choices=["xgboost", "pytorch"])
    parser.add_argument("--tracking-uri",  default=os.environ.get("MLFLOW_TRACKING_URI"))
    parser.add_argument("--output-file",   default="gate_result.json")
    return parser.parse_args()


def download_model_artifacts(run_id: str, tracking_uri: str, model_type: str) -> Path:
    """
    Download model artifacts from MLflow (which proxies S3).
    Returns local path to downloaded artifacts.
    """
    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.tracking.MlflowClient()

    local_dir = Path(f"/tmp/model_artifacts_{run_id[:8]}")
    local_dir.mkdir(parents=True, exist_ok=True)

    # Download model artifacts
    for artifact in ["inference_metadata.json", "preprocessor.pkl"]:
        client.download_artifacts(run_id, f"model/{artifact}", str(local_dir))

    if model_type == "xgboost":
        client.download_artifacts(run_id, "model/model.json", str(local_dir))
    else:
        client.download_artifacts(run_id, "model/model_scripted.pt", str(local_dir))

    log.info(f"Downloaded artifacts to {local_dir}")
    return local_dir


def load_model(artifact_dir: Path, model_type: str):
    """Load model and preprocessor from artifact directory."""
    with open(artifact_dir / "preprocessor.pkl", "rb") as f:
        preprocessor = pickle.load(f)

    with open(artifact_dir / "inference_metadata.json") as f:
        metadata = json.load(f)

    if model_type == "xgboost":
        booster = xgb.Booster()
        booster.load_model(str(artifact_dir / "model.json"))
        return booster, preprocessor, metadata
    else:
        import torch
        model = torch.jit.load(str(artifact_dir / "model_scripted.pt"))
        model.eval()
        return model, preprocessor, metadata


def load_test_data(s3_path: str) -> pd.DataFrame:
    """Load held-out test data from S3."""
    import awswrangler as wr
    log.info(f"Loading test data from: {s3_path}")
    df = wr.s3.read_parquet(path=s3_path)
    log.info(f"Test set: {len(df):,} rows | Churn rate: {df['is_churned'].mean():.1%}")
    return df


def preprocess_test_data(df: pd.DataFrame, preprocessor: dict, metadata: dict) -> tuple:
    """Apply the same preprocessing as training (using fitted scaler/encoders)."""
    from common.data_loader import NUMERIC_FEATURES, CATEGORICAL_FEATURES

    df = df.copy()
    scaler   = preprocessor["scaler"]
    encoders = preprocessor["encoders"]

    for col in NUMERIC_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna(0.0)
    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            df[col] = df[col].fillna("unknown")

    encoded_cats = []
    for col in CATEGORICAL_FEATURES:
        if col not in df.columns or col not in encoders:
            continue
        enc   = encoders[col]
        known = set(enc.classes_)
        df[col] = df[col].apply(lambda x: x if x in known else "unknown")
        encoded_cats.append(enc.transform(df[col].astype(str)).reshape(-1, 1))

    numeric_scaled = scaler.transform(df[NUMERIC_FEATURES].values.astype(np.float32))
    if encoded_cats:
        X = np.hstack([numeric_scaled] + encoded_cats).astype(np.float32)
    else:
        X = numeric_scaled

    y = df["is_churned"].values.astype(np.int32)
    return X, y


def run_inference_with_latency(model, X: np.ndarray, model_type: str, metadata: dict) -> tuple:
    """
    Run inference and measure latency.
    We measure P99 latency because tail latency is what SLAs are based on.
    Users who see the churn prediction UI should get results in <200ms.
    """
    threshold = metadata.get("threshold", 0.5)
    batch_size = 32  # Simulate realistic real-time batch size

    all_probas   = []
    latencies_ms = []

    for start_idx in range(0, len(X), batch_size):
        batch = X[start_idx:start_idx + batch_size]

        t0 = time.perf_counter()
        if model_type == "xgboost":
            dmatrix = xgb.DMatrix(batch, feature_names=metadata.get("feature_names"))
            proba   = model.predict(dmatrix)
        else:
            import torch
            with torch.no_grad():
                tensor = torch.tensor(batch, dtype=torch.float32)
                logits = model(tensor).squeeze(1)
                proba  = torch.sigmoid(logits).numpy()

        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies_ms.append(elapsed_ms)
        all_probas.extend(proba.tolist())

    p50 = float(np.percentile(latencies_ms, 50))
    p99 = float(np.percentile(latencies_ms, 99))
    log.info(f"Latency — P50: {p50:.1f}ms | P99: {p99:.1f}ms")

    probas  = np.array(all_probas)
    binary  = (probas >= threshold).astype(int)
    return probas, binary, p50, p99


def evaluate_quality_gates(
    y_true: np.ndarray,
    y_proba: np.ndarray,
    y_binary: np.ndarray,
    p99_latency_ms: float,
) -> tuple[dict, list[str]]:
    """
    Compute metrics and check quality gates.
    Returns (metrics_dict, list_of_failed_gates).
    """
    from sklearn.metrics import classification_report, roc_auc_score

    auc    = roc_auc_score(y_true, y_proba)
    report = classification_report(y_true, y_binary, output_dict=True, zero_division=0)

    metrics = {
        "test_auc":           auc,
        "test_precision":     report["1"]["precision"],
        "test_recall":        report["1"]["recall"],
        "test_f1":            report["1"]["f1-score"],
        "test_accuracy":      report["accuracy"],
        "test_p99_latency_ms": p99_latency_ms,
        "test_samples":       len(y_true),
        "test_churn_rate":    float(y_true.mean()),
    }

    failures = []
    gate_map = {
        "test_auc":            ("auc",           QUALITY_GATES["auc"]),
        "test_precision":      ("precision",      QUALITY_GATES["precision"]),
        "test_recall":         ("recall",         QUALITY_GATES["recall"]),
        "test_p99_latency_ms": ("p99_latency_ms", QUALITY_GATES["p99_latency_ms"]),
    }

    for metric_key, (gate_name, threshold) in gate_map.items():
        value     = metrics[metric_key]
        # Latency gate is "must be LESS THAN threshold"; others are "must be GREATER"
        if gate_name == "p99_latency_ms":
            passed = value < threshold
        else:
            passed = value >= threshold

        status = "PASS" if passed else "FAIL"
        log.info(f"  [{status}] {gate_name}: {value:.4f} (threshold: {threshold})")
        if not passed:
            failures.append(f"{gate_name}: {value:.4f} < {threshold}")

    return metrics, failures


def main():
    args = parse_args()

    # Add common/ to path for shared utilities
    sys.path.insert(0, str(Path(__file__).parent.parent / "training"))

    # ── Download + load model ──────────────────────────────────────────────────
    artifact_dir             = download_model_artifacts(args.run_id, args.tracking_uri, args.model_type)
    model, preprocessor, metadata = load_model(artifact_dir, args.model_type)

    # ── Load and preprocess test data ──────────────────────────────────────────
    df    = load_test_data(args.test_data_s3)
    X, y  = preprocess_test_data(df, preprocessor, metadata)

    # ── Run inference with latency measurement ─────────────────────────────────
    y_proba, y_binary, p50, p99 = run_inference_with_latency(
        model, X, args.model_type, metadata
    )

    # ── Quality gate evaluation ────────────────────────────────────────────────
    metrics, failures = evaluate_quality_gates(y, y_proba, y_binary, p99)

    # ── Log test metrics to the original MLflow run ────────────────────────────
    mlflow.set_tracking_uri(args.tracking_uri)
    with mlflow.start_run(run_id=args.run_id):
        mlflow.log_metrics(metrics)
        mlflow.log_metric("test_p50_latency_ms", p50)
        mlflow.set_tag("quality_gate_status", "PASS" if not failures else "FAIL")
        if failures:
            mlflow.set_tag("quality_gate_failures", "; ".join(failures))

    # ── Write gate result for GitHub Actions ──────────────────────────────────
    gate_result = {
        "run_id":    args.run_id,
        "model_type": args.model_type,
        "passed":    not failures,
        "metrics":   metrics,
        "failures":  failures,
        "thresholds": QUALITY_GATES,
    }
    Path(args.output_file).write_text(json.dumps(gate_result, indent=2))

    if failures:
        log.error(f"Quality gate FAILED: {failures}")
        print(f"::set-output name=gate_passed::false")
        sys.exit(1)
    else:
        log.info(f"Quality gate PASSED — AUC={metrics['test_auc']:.4f}")
        print(f"::set-output name=gate_passed::true")
        print(f"::set-output name=test_auc::{metrics['test_auc']:.6f}")
        sys.exit(0)


if __name__ == "__main__":
    main()
