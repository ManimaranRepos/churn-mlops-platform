"""
MLflow logging utilities shared across all training scripts.
Centralises: experiment setup, metric logging, artifact saving, model registration.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server environments
import matplotlib.pyplot as plt
import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    classification_report,
    confusion_matrix,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)

log = logging.getLogger(__name__)


def setup_mlflow(
    tracking_uri: str,
    experiment_name: str,
    run_name: Optional[str] = None,
    tags: Optional[dict] = None,
) -> mlflow.ActiveRun:
    """
    Configure MLflow and start a tracked run.
    All subsequent mlflow.log_* calls go to this run.
    """
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)

    default_tags = {
        "git_sha":     os.environ.get("GIT_SHA", "unknown"),
        "sagemaker_job": os.environ.get("TRAINING_JOB_NAME", "local"),
        "environment": os.environ.get("ENVIRONMENT", "dev"),
    }
    if tags:
        default_tags.update(tags)

    return mlflow.start_run(run_name=run_name, tags=default_tags)


def log_training_params(params: dict[str, Any]) -> None:
    """Log hyperparameters. Called once at the start of training."""
    mlflow.log_params(params)
    log.info(f"Logged {len(params)} hyperparameters to MLflow")


def log_metrics_at_step(metrics: dict[str, float], step: int) -> None:
    """Log metrics at a training step (e.g., per epoch or per boosting round)."""
    mlflow.log_metrics(metrics, step=step)


def compute_and_log_evaluation_metrics(
    y_true: np.ndarray,
    y_pred_proba: np.ndarray,
    y_pred_binary: np.ndarray,
    split_name: str = "test",
    threshold: float = 0.5,
) -> dict[str, float]:
    """
    Compute standard binary classification metrics and log them to MLflow.
    Also generates and saves diagnostic plots as MLflow artifacts.

    Args:
        y_true: ground truth labels (0/1)
        y_pred_proba: predicted probabilities (float)
        y_pred_binary: binarized predictions at threshold
        split_name: 'val' or 'test' (used as metric prefix)
        threshold: classification threshold (default 0.5)

    Returns:
        dict of all computed metrics
    """
    # ── Core metrics ──────────────────────────────────────────────────────────
    auc = roc_auc_score(y_true, y_pred_proba)
    report = classification_report(y_true, y_pred_binary, output_dict=True)

    metrics = {
        f"{split_name}_auc":       auc,
        f"{split_name}_precision": report["1"]["precision"],
        f"{split_name}_recall":    report["1"]["recall"],
        f"{split_name}_f1":        report["1"]["f1-score"],
        f"{split_name}_accuracy":  report["accuracy"],
        f"{split_name}_threshold": threshold,
        # False negative rate: churners we missed (expensive — they churn silently)
        f"{split_name}_fnr":       1.0 - report["1"]["recall"],
        # False positive rate: customers wrongly labelled as churning (gets intervention)
        f"{split_name}_fpr":       report["0"]["recall"] if "0" in report else 0.0,
    }

    mlflow.log_metrics(metrics)

    # ── Diagnostic plots ──────────────────────────────────────────────────────
    _save_roc_curve(y_true, y_pred_proba, auc, split_name)
    _save_precision_recall_curve(y_true, y_pred_proba, split_name)
    _save_confusion_matrix(y_true, y_pred_binary, split_name)

    log.info(
        f"[{split_name}] AUC={auc:.4f} | "
        f"Precision={metrics[f'{split_name}_precision']:.4f} | "
        f"Recall={metrics[f'{split_name}_recall']:.4f} | "
        f"F1={metrics[f'{split_name}_f1']:.4f}"
    )
    return metrics


def _save_roc_curve(y_true, y_pred_proba, auc, split_name):
    fpr, tpr, _ = roc_curve(y_true, y_pred_proba)
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(fpr, tpr, label=f"AUC = {auc:.4f}")
    ax.plot([0, 1], [0, 1], "k--", label="Random")
    ax.set_xlabel("False Positive Rate")
    ax.set_ylabel("True Positive Rate")
    ax.set_title(f"ROC Curve ({split_name})")
    ax.legend()
    mlflow.log_figure(fig, f"plots/{split_name}_roc_curve.png")
    plt.close(fig)


def _save_precision_recall_curve(y_true, y_pred_proba, split_name):
    precision, recall, thresholds = precision_recall_curve(y_true, y_pred_proba)
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    axes[0].plot(recall, precision)
    axes[0].set_xlabel("Recall")
    axes[0].set_ylabel("Precision")
    axes[0].set_title(f"Precision-Recall Curve ({split_name})")

    # Threshold plot: helps choose operating point
    f1_scores = 2 * precision[:-1] * recall[:-1] / (precision[:-1] + recall[:-1] + 1e-9)
    axes[1].plot(thresholds, precision[:-1], label="Precision")
    axes[1].plot(thresholds, recall[:-1],    label="Recall")
    axes[1].plot(thresholds, f1_scores,      label="F1")
    axes[1].set_xlabel("Threshold")
    axes[1].set_title("Metrics vs Threshold")
    axes[1].legend()

    mlflow.log_figure(fig, f"plots/{split_name}_precision_recall.png")
    plt.close(fig)


def _save_confusion_matrix(y_true, y_pred_binary, split_name):
    fig, ax = plt.subplots(figsize=(6, 5))
    cm = confusion_matrix(y_true, y_pred_binary)
    ConfusionMatrixDisplay(cm, display_labels=["Active", "Churned"]).plot(ax=ax)
    ax.set_title(f"Confusion Matrix ({split_name})")
    mlflow.log_figure(fig, f"plots/{split_name}_confusion_matrix.png")
    plt.close(fig)


def log_feature_importance(
    feature_names: list[str],
    importance_scores: np.ndarray,
    importance_type: str = "gain",
) -> None:
    """
    Log and plot feature importances.
    Why this matters: stakeholders want to know WHICH behaviours drive churn.
    'Feature X has importance 0.35' → 'session_duration_trend is the top churn signal'
    """
    importance_df = pd.DataFrame({
        "feature":    feature_names,
        "importance": importance_scores,
    }).sort_values("importance", ascending=False)

    # Save as CSV for downstream analysis
    mlflow.log_text(
        importance_df.to_csv(index=False),
        "feature_importance.csv"
    )

    # Top 20 features bar chart
    top_n = min(20, len(importance_df))
    fig, ax = plt.subplots(figsize=(10, 8))
    top_features = importance_df.head(top_n)
    ax.barh(top_features["feature"][::-1], top_features["importance"][::-1])
    ax.set_xlabel(f"Feature Importance ({importance_type})")
    ax.set_title(f"Top {top_n} Features by {importance_type.title()}")
    plt.tight_layout()
    mlflow.log_figure(fig, "plots/feature_importance.png")
    plt.close(fig)

    # Log top 5 importances as individual metrics (visible in MLflow UI table)
    for i, row in enumerate(importance_df.head(5).itertuples()):
        mlflow.log_metric(f"top_feature_{i+1}_importance", float(row.importance))
        mlflow.log_param(f"top_feature_{i+1}_name", row.feature)


def register_model_to_mlflow(
    model,
    model_name: str,
    artifact_path: str,
    run_id: str,
    metrics: dict,
    flavor: str = "sklearn",
) -> str:
    """
    Register the trained model in MLflow Model Registry.
    Returns the registered model version.
    """
    model_uri = f"runs:/{run_id}/{artifact_path}"

    registered = mlflow.register_model(
        model_uri=model_uri,
        name=model_name,
    )

    client = mlflow.tracking.MlflowClient()

    # Tag the version with key metrics for quick filtering
    for metric_name, value in metrics.items():
        if isinstance(value, (int, float)):
            client.set_model_version_tag(
                name=model_name,
                version=registered.version,
                key=metric_name,
                value=str(round(value, 4)),
            )

    log.info(
        f"Registered model '{model_name}' version {registered.version} "
        f"(AUC: {metrics.get('test_auc', 'N/A')})"
    )
    return registered.version
