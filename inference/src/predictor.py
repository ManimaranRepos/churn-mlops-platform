"""
Model loading and prediction logic.

WHY load both XGBoost and PyTorch?
  The training pipeline picks a winner per run, but the winner changes between
  training iterations. Rather than baking the model type into the container image
  (which would require a redeploy on every model promotion), the predictor:
    1. Reads the current Production model name from an env var (MODEL_NAME)
    2. Downloads that model from MLflow Model Registry at startup
    3. Inspects metadata to determine whether it's XGBoost or PyTorch
    4. Loads the appropriate runtime (xgboost or torch.jit)

  This means a new model can be deployed by:
    a. Promoting it in MLflow (changes the Production alias)
    b. Rolling the Kubernetes Deployment (triggers fresh model download)
  No Docker image rebuild needed.

Prediction flow:
  raw features → preprocessor.pkl (scikit-learn) → model → raw score
  → threshold (from inference_metadata.json) → churn probability + binary label

Thread safety:
  The model and preprocessor are loaded once at startup (in lifespan()) and
  stored as module-level singletons. FastAPI's async handlers read them
  without mutation — no locks needed (reads are thread-safe in Python).
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

MLFLOW_TRACKING_URI = os.environ["MLFLOW_TRACKING_URI"]
MODEL_NAME          = os.getenv("MODEL_NAME", "churn-prediction")
MODEL_STAGE         = os.getenv("MODEL_STAGE", "Production")
FEATURE_COLUMNS_ENV = os.getenv("FEATURE_COLUMNS", "")

mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)


@dataclass
class LoadedModel:
    model_type:    str          # "xgboost" or "pytorch"
    model:         Any          # XGBClassifier or torch.jit.ScriptModule
    preprocessor:  Any          # sklearn Pipeline
    threshold:     float        # F1-optimal threshold from training
    feature_cols:  list[str]    # Expected input column order
    model_version: str
    model_uri:     str


_loaded_model: LoadedModel | None = None


def get_model() -> LoadedModel:
    if _loaded_model is None:
        raise RuntimeError("Model not loaded — call load_model() during startup")
    return _loaded_model


def load_model() -> LoadedModel:
    """
    Download the Production model from MLflow and load it into memory.
    Called once during FastAPI lifespan startup.

    Downloads to a temp directory — the container has /tmp with ~500MB available.
    The model artifacts are:
      - model.json or best_model_weights.pt (the actual model)
      - preprocessor.pkl (sklearn Pipeline)
      - inference_metadata.json (threshold, feature_cols, model_type)
    """
    global _loaded_model

    log.info(f"Loading model '{MODEL_NAME}' stage='{MODEL_STAGE}' from MLflow...")
    t0 = time.time()

    client    = mlflow.tracking.MlflowClient()
    versions  = client.get_latest_versions(MODEL_NAME, stages=[MODEL_STAGE])
    if not versions:
        raise RuntimeError(f"No '{MODEL_STAGE}' version found for model '{MODEL_NAME}'")

    mv        = versions[0]
    model_uri = mv.source
    log.info(f"Model URI: {model_uri} (version {mv.version})")

    with tempfile.TemporaryDirectory() as tmpdir:
        artifact_path = mlflow.artifacts.download_artifacts(model_uri, dst_path=tmpdir)
        artifact_dir  = Path(artifact_path)

        metadata_path = artifact_dir / "inference_metadata.json"
        with open(metadata_path) as f:
            metadata = json.load(f)

        model_type   = metadata["model_type"]           # "xgboost" or "pytorch"
        threshold    = float(metadata["threshold"])
        feature_cols = metadata["feature_columns"]

        preprocessor_path = artifact_dir / "preprocessor.pkl"
        with open(preprocessor_path, "rb") as f:
            preprocessor = pickle.load(f)

        if model_type == "xgboost":
            import xgboost as xgb
            model_path = artifact_dir / "model.json"
            model      = xgb.XGBClassifier()
            model.load_model(str(model_path))
            log.info(f"XGBoost model loaded (n_estimators={model.n_estimators})")

        elif model_type == "pytorch":
            import torch
            model_path = artifact_dir / "model_scripted.pt"
            model      = torch.jit.load(str(model_path), map_location="cpu")
            model.eval()
            log.info("PyTorch TorchScript model loaded (CPU inference)")

        else:
            raise ValueError(f"Unknown model_type: {model_type}")

    elapsed = time.time() - t0
    log.info(f"Model loaded in {elapsed:.1f}s (threshold={threshold:.3f})")

    _loaded_model = LoadedModel(
        model_type    = model_type,
        model         = model,
        preprocessor  = preprocessor,
        threshold     = threshold,
        feature_cols  = feature_cols,
        model_version = mv.version,
        model_uri     = model_uri,
    )
    return _loaded_model


def predict(features: dict[str, Any], loaded: LoadedModel) -> dict[str, Any]:
    """
    Run inference on a single customer's feature dict.

    Returns:
      churn_probability: float  — raw model score (0.0–1.0)
      churn_prediction:  bool   — True if score > threshold
      model_version:     str    — for traceability
      threshold:         float  — the threshold used
    """
    df = pd.DataFrame([features], columns=loaded.feature_cols)

    missing = set(loaded.feature_cols) - set(features.keys())
    if missing:
        raise ValueError(f"Missing required features: {sorted(missing)}")

    df = df[loaded.feature_cols]

    X_transformed = loaded.preprocessor.transform(df)

    if loaded.model_type == "xgboost":
        proba = loaded.model.predict_proba(X_transformed)[0, 1]

    elif loaded.model_type == "pytorch":
        import torch
        tensor = torch.tensor(X_transformed, dtype=torch.float32)
        with torch.no_grad():
            logits = loaded.model(tensor)
            proba  = float(torch.sigmoid(logits).squeeze().item())

    proba = float(np.clip(proba, 0.0, 1.0))

    return {
        "churn_probability": round(proba, 6),
        "churn_prediction":  proba >= loaded.threshold,
        "model_version":     loaded.model_version,
        "threshold":         loaded.threshold,
        "model_type":        loaded.model_type,
    }


def predict_batch(feature_rows: list[dict[str, Any]], loaded: LoadedModel) -> list[dict[str, Any]]:
    """
    Batch prediction — processes all rows in a single model call (much faster
    than calling predict() in a loop for XGBoost due to tree traversal overhead).
    """
    df = pd.DataFrame(feature_rows, columns=loaded.feature_cols)[loaded.feature_cols]
    X_transformed = loaded.preprocessor.transform(df)

    if loaded.model_type == "xgboost":
        probas = loaded.model.predict_proba(X_transformed)[:, 1]

    elif loaded.model_type == "pytorch":
        import torch
        tensor = torch.tensor(X_transformed, dtype=torch.float32)
        with torch.no_grad():
            logits = loaded.model(tensor)
            probas = torch.sigmoid(logits).squeeze().numpy()
        if probas.ndim == 0:
            probas = probas.reshape(1)

    results = []
    for i, proba in enumerate(probas):
        p = float(np.clip(proba, 0.0, 1.0))
        results.append({
            "churn_probability": round(p, 6),
            "churn_prediction":  p >= loaded.threshold,
            "model_version":     loaded.model_version,
            "threshold":         loaded.threshold,
            "model_type":        loaded.model_type,
        })
    return results
