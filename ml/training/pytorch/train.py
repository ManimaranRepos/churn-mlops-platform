"""
PyTorch MLP churn classifier — SageMaker Training Job entry point.

Same SageMaker contract as XGBoost:
  - Hyperparams from /opt/ml/input/config/hyperparameters.json
  - Data from /opt/ml/input/data/train/ and /validation/
  - Artifacts to /opt/ml/model/
"""

import json
import logging
import os
import pickle
import sys
import time
from pathlib import Path

import mlflow
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.data_loader import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    load_features_from_s3,
    preprocess_features,
)
from common.mlflow_utils import (
    compute_and_log_evaluation_metrics,
    log_training_params,
    register_model_to_mlflow,
    setup_mlflow,
)
from model import ChurnMLP, FocalLoss

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

SM_INPUT_DIR   = Path(os.environ.get("SM_INPUT_DIR",  "/opt/ml/input"))
SM_MODEL_DIR   = Path(os.environ.get("SM_MODEL_DIR",  "/opt/ml/model"))
SM_OUTPUT_DIR  = Path(os.environ.get("SM_OUTPUT_DIR", "/opt/ml/output"))
SM_CHANNEL_DIR = SM_INPUT_DIR / "data"

DEFAULTS = {
    "hidden_dims":    "256,128,64",  # Comma-separated string (JSON doesn't allow lists as SM hyperparams)
    "dropout_rate":   0.3,
    "learning_rate":  1e-3,
    "weight_decay":   1e-4,          # L2 regularisation (prevents overfitting)
    "batch_size":     512,
    "max_epochs":     100,
    "patience":       10,             # Early stopping: stop if val_auc doesn't improve for 10 epochs
    "focal_gamma":    2.0,
    "seed":           42,
}


def load_hyperparameters() -> dict:
    hp_path = SM_INPUT_DIR / "config" / "hyperparameters.json"
    params = DEFAULTS.copy()

    if hp_path.exists():
        with open(hp_path) as f:
            raw = json.load(f)
        for key, value in raw.items():
            if key not in params:
                continue
            default = DEFAULTS[key]
            if isinstance(default, int):
                params[key] = int(value)
            elif isinstance(default, float):
                params[key] = float(value)
            else:
                params[key] = value

    params["hidden_dims"] = [int(x) for x in str(params["hidden_dims"]).split(",")]
    return params


def load_data(params: dict):
    def find_parquets(directory: Path) -> str:
        if not directory.exists():
            raise FileNotFoundError(f"Channel not found: {directory}")
        files = list(directory.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No parquet files in {directory}")
        return str(directory)

    df_train = load_features_from_s3(find_parquets(SM_CHANNEL_DIR / "train"))
    df_val   = load_features_from_s3(find_parquets(SM_CHANNEL_DIR / "validation"))

    X_train, y_train, scaler, encoders = preprocess_features(df_train, fit=True)
    X_val,   y_val,   _,      _        = preprocess_features(df_val, scaler=scaler, encoders=encoders, fit=False)

    return X_train, y_train, X_val, y_val, scaler, encoders


def build_dataloaders(X_train, y_train, X_val, y_val, batch_size: int):
    def to_tensors(X, y):
        return TensorDataset(
            torch.tensor(X, dtype=torch.float32),
            torch.tensor(y, dtype=torch.long),
        )

    train_loader = DataLoader(to_tensors(X_train, y_train), batch_size=batch_size, shuffle=True,  drop_last=True)
    val_loader   = DataLoader(to_tensors(X_val,   y_val),   batch_size=batch_size, shuffle=False)
    return train_loader, val_loader


def compute_pos_weight(y_train: np.ndarray) -> float:
    """Compute positive class weight for imbalanced data."""
    n_neg = (y_train == 0).sum()
    n_pos = (y_train == 1).sum()
    weight = n_neg / n_pos
    log.info(f"Positive class weight: {weight:.2f}")
    return float(weight)


def train_epoch(model, loader, optimizer, criterion, device) -> dict[str, float]:
    model.train()
    total_loss = 0.0
    all_logits, all_labels = [], []

    for X_batch, y_batch in loader:
        X_batch = X_batch.to(device)
        y_batch = y_batch.to(device)

        optimizer.zero_grad()
        logits = model(X_batch).squeeze(1)
        loss   = criterion(logits, y_batch)
        loss.backward()

        # Gradient clipping: prevents occasional large gradient updates from derailing training
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

        optimizer.step()
        total_loss += loss.item() * len(X_batch)

        all_logits.append(logits.detach().cpu())
        all_labels.append(y_batch.detach().cpu())

    all_logits = torch.cat(all_logits)
    all_labels = torch.cat(all_labels)
    all_proba  = torch.sigmoid(all_logits).numpy()
    all_labels = all_labels.numpy()

    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(all_labels, all_proba) if all_labels.sum() > 0 else 0.0

    return {
        "train_loss": total_loss / len(loader.dataset),
        "train_auc":  auc,
    }


@torch.no_grad()
def eval_epoch(model, loader, criterion, device) -> dict[str, float]:
    model.eval()
    total_loss = 0.0
    all_proba, all_labels = [], []

    for X_batch, y_batch in loader:
        X_batch = X_batch.to(device)
        y_batch = y_batch.to(device)

        logits = model(X_batch).squeeze(1)
        loss   = criterion(logits, y_batch)
        total_loss += loss.item() * len(X_batch)

        all_proba.append(torch.sigmoid(logits).cpu())
        all_labels.append(y_batch.cpu())

    all_proba  = torch.cat(all_proba).numpy()
    all_labels = torch.cat(all_labels).numpy()

    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(all_labels, all_proba) if all_labels.sum() > 0 else 0.0

    return {
        "val_loss": total_loss / len(loader.dataset),
        "val_auc":  auc,
        "_proba":   all_proba,   # Keep for final evaluation (not logged directly)
        "_labels":  all_labels,
    }


def train(params: dict) -> dict:
    SM_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(params["seed"])
    np.random.seed(params["seed"])

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log.info(f"Training on device: {device}")

    X_train, y_train, X_val, y_val, scaler, encoders = load_data(params)
    pos_weight = compute_pos_weight(y_train)

    train_loader, val_loader = build_dataloaders(
        X_train, y_train, X_val, y_val, params["batch_size"]
    )

    model = ChurnMLP(
        input_dim=X_train.shape[1],
        hidden_dims=params["hidden_dims"],
        dropout_rate=params["dropout_rate"],
    ).to(device)

    criterion = FocalLoss(alpha=pos_weight, gamma=params["focal_gamma"])

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=params["learning_rate"],
        weight_decay=params["weight_decay"],
    )
    # ReduceLROnPlateau: halve LR when val_auc stops improving for 5 epochs
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="max", factor=0.5, patience=5, verbose=True
    )

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow.mlops.svc.cluster.local:5000")
    experiment   = os.environ.get("MLFLOW_EXPERIMENT_NAME", "churn-prediction-pytorch")
    run_name     = f"mlp-{os.environ.get('TRAINING_JOB_NAME', 'local')}"

    with setup_mlflow(tracking_uri, experiment, run_name=run_name, tags={"model_type": "pytorch_mlp"}):
        run_id = mlflow.active_run().info.run_id
        log_training_params({
            **{k: v if not isinstance(v, list) else str(v) for k, v in params.items()},
            "input_dim":      X_train.shape[1],
            "train_samples":  len(X_train),
            "val_samples":    len(X_val),
            "device":         str(device),
            "pos_weight":     pos_weight,
        })

        best_val_auc    = 0.0
        best_epoch      = 0
        patience_counter = 0
        best_state_path  = SM_MODEL_DIR / "best_model_weights.pt"

        log.info(f"Starting training for up to {params['max_epochs']} epochs...")
        start = time.time()

        for epoch in range(1, params["max_epochs"] + 1):
            train_metrics = train_epoch(model, train_loader, optimizer, criterion, device)
            val_metrics   = eval_epoch(model, val_loader, criterion, device)

            current_auc = val_metrics["val_auc"]
            scheduler.step(current_auc)

            # Log to MLflow (exclude internal keys starting with _)
            step_metrics = {**train_metrics, "val_loss": val_metrics["val_loss"], "val_auc": current_auc}
            mlflow.log_metrics(step_metrics, step=epoch)

            if current_auc > best_val_auc:
                best_val_auc = current_auc
                best_epoch   = epoch
                patience_counter = 0
                torch.save(model.state_dict(), best_state_path)
                log.info(f"Epoch {epoch:3d} ↑ val_auc={current_auc:.4f} (new best)")
            else:
                patience_counter += 1
                if patience_counter >= params["patience"]:
                    log.info(f"Early stopping at epoch {epoch} (patience={params['patience']})")
                    break

        elapsed = time.time() - start
        log.info(f"Training complete in {elapsed:.1f}s | Best epoch: {best_epoch} | AUC: {best_val_auc:.4f}")

        # ── Load best weights and do final eval ────────────────────────────────
        model.load_state_dict(torch.load(best_state_path, map_location=device))
        final_eval = eval_epoch(model, val_loader, criterion, device)

        val_proba  = final_eval["_proba"]
        val_labels = final_eval["_labels"]

        # Find optimal threshold
        from sklearn.metrics import f1_score
        thresholds = np.arange(0.1, 0.9, 0.01)
        best_t, best_f1 = 0.5, 0.0
        for t in thresholds:
            f1 = f1_score(val_labels, (val_proba >= t).astype(int), zero_division=0)
            if f1 > best_f1:
                best_f1, best_t = f1, t

        mlflow.log_metric("optimal_threshold", best_t)
        mlflow.log_metric("training_time_seconds", elapsed)
        mlflow.log_metric("best_epoch", best_epoch)

        val_binary   = (val_proba >= best_t).astype(int)
        final_metrics = compute_and_log_evaluation_metrics(
            val_labels, val_proba, val_binary, split_name="val", threshold=best_t
        )

        # ── Save artifacts ─────────────────────────────────────────────────────
        _save_artifacts(model, scaler, encoders, best_t, X_train.shape[1], params, SM_MODEL_DIR)
        mlflow.log_artifacts(str(SM_MODEL_DIR), artifact_path="model")

        version = register_model_to_mlflow(
            model=model,
            model_name="churn-prediction-pytorch",
            artifact_path="model",
            run_id=run_id,
            metrics=final_metrics,
        )

        print(f"val:auc: {final_metrics['val_auc']:.6f}")
        print(f"val:precision: {final_metrics['val_precision']:.6f}")
        print(f"val:recall: {final_metrics['val_recall']:.6f}")
        print(f"val:f1: {final_metrics['val_f1']:.6f}")

        log.info(f"AUC={final_metrics['val_auc']:.4f} | Model version: {version}")

    return final_metrics


def _save_artifacts(model, scaler, encoders, threshold, input_dim, params, output_dir: Path):
    # TorchScript: converts model to a static computation graph.
    # The inference container can load this without needing the class definition.
    dummy = torch.zeros(1, input_dim)
    scripted = torch.jit.trace(model.cpu().eval(), dummy)
    scripted.save(str(output_dir / "model_scripted.pt"))

    with open(output_dir / "preprocessor.pkl", "wb") as f:
        pickle.dump({"scaler": scaler, "encoders": encoders}, f)

    metadata = {
        "model_type":           "pytorch_mlp",
        "threshold":            threshold,
        "numeric_features":     NUMERIC_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
        "feature_names":        NUMERIC_FEATURES + [f"{c}_encoded" for c in CATEGORICAL_FEATURES],
        "hidden_dims":          params["hidden_dims"],
        "input_dim":            input_dim,
        "training_job":         os.environ.get("TRAINING_JOB_NAME", "local"),
        "git_sha":              os.environ.get("GIT_SHA", "unknown"),
    }
    with open(output_dir / "inference_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    log.info("Saved model_scripted.pt, preprocessor.pkl, inference_metadata.json")


if __name__ == "__main__":
    params = load_hyperparameters()
    train(params)
    sys.exit(0)
