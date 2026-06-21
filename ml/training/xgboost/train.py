"""
XGBoost churn classifier — SageMaker Training Job entry point.

WHY XGBoost first?
  XGBoost is the go-to baseline for tabular churn prediction:
  - Handles missing values natively (no imputation needed)
  - Feature importance via SHAP is interpretable to non-ML stakeholders
  - Trains in <5 min on this feature set (fast iteration cycle)
  - Consistently wins Kaggle churn competitions

  We train here first, then compare against the PyTorch MLP.
  The better AUC model gets registered to production.

SageMaker contract:
  - Hyperparams come from /opt/ml/input/config/hyperparameters.json
  - Training data comes from /opt/ml/input/data/train/ and /validation/
  - Model artifacts are saved to /opt/ml/model/ (SageMaker tars and uploads to S3)
  - Metrics are written to stdout as "metric_name: value" for CloudWatch extraction
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
import xgboost as xgb

# Allow importing from common/ when running inside SageMaker container
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.data_loader import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    load_features_from_s3,
    preprocess_features,
    split_dataset,
)
from common.mlflow_utils import (
    compute_and_log_evaluation_metrics,
    log_feature_importance,
    log_training_params,
    register_model_to_mlflow,
    setup_mlflow,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── SageMaker paths ────────────────────────────────────────────────────────────
SM_INPUT_DIR   = Path(os.environ.get("SM_INPUT_DIR",   "/opt/ml/input"))
SM_MODEL_DIR   = Path(os.environ.get("SM_MODEL_DIR",   "/opt/ml/model"))
SM_OUTPUT_DIR  = Path(os.environ.get("SM_OUTPUT_DIR",  "/opt/ml/output"))
SM_CHANNEL_DIR = SM_INPUT_DIR / "data"

# ── Default hyperparameters ────────────────────────────────────────────────────
# These are overridden by SageMaker HPO or explicit hyperparameter dict.
DEFAULTS = {
    # Tree structure
    "max_depth":         6,
    "min_child_weight":  5,     # Prevents learning from tiny leaf nodes
    "gamma":             0.1,   # Min loss reduction to make a split
    "subsample":         0.8,   # Row sampling per tree (reduces overfitting)
    "colsample_bytree":  0.8,   # Feature sampling per tree

    # Learning
    "learning_rate":     0.05,  # Lower = slower but more stable
    "n_estimators":      500,
    "early_stopping_rounds": 30,

    # Churn-specific: class imbalance (~10% churn rate)
    # scale_pos_weight = count(negative) / count(positive)
    # Tells XGBoost to weight the minority class higher
    "scale_pos_weight":  "auto",

    # Training config
    "eval_metric":       "auc",  # Optimise for AUC (not accuracy — imbalanced dataset)
    "tree_method":       "hist", # Faster histogram-based split finding (vs 'exact')
    "seed":              42,
}


def load_hyperparameters() -> dict:
    """
    SageMaker passes hyperparameters as a JSON file.
    All values arrive as strings — we convert to correct types here.
    """
    hp_path = SM_INPUT_DIR / "config" / "hyperparameters.json"

    if hp_path.exists():
        with open(hp_path) as f:
            raw = json.load(f)
        log.info(f"Loaded {len(raw)} hyperparameters from SageMaker config")
    else:
        log.info("No hyperparameters.json found — using defaults (likely local run)")
        raw = {}

    params = DEFAULTS.copy()
    for key, value in raw.items():
        if key not in params:
            continue
        default = DEFAULTS[key]
        if isinstance(default, int):
            params[key] = int(value)
        elif isinstance(default, float):
            params[key] = float(value)
        else:
            params[key] = value  # string (eval_metric, tree_method, etc.)

    return params


def load_data() -> tuple:
    """
    Load training and validation data from SageMaker input channels.

    SageMaker copies each channel's S3 content into:
      /opt/ml/input/data/<channel_name>/

    We use two channels: 'train' and 'validation' (pre-split by the DAG).
    If running locally, fall back to a single file at SM_CHANNEL_DIR/train/.
    """
    train_dir = SM_CHANNEL_DIR / "train"
    val_dir   = SM_CHANNEL_DIR / "validation"

    # Collect all parquet files in the channel directory
    def find_parquets(directory: Path) -> str:
        """Return the directory path (awswrangler reads all parquets in a dir)."""
        if not directory.exists():
            raise FileNotFoundError(f"Input channel not found: {directory}")
        files = list(directory.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No parquet files in {directory}")
        log.info(f"Found {len(files)} parquet file(s) in {directory}")
        return str(directory)

    df_train = load_features_from_s3(find_parquets(train_dir))
    df_val   = load_features_from_s3(find_parquets(val_dir))

    # Preprocess train (fits scaler/encoders), then transform val with same params
    X_train, y_train, scaler, encoders = preprocess_features(df_train, fit=True)
    X_val,   y_val,   _,      _        = preprocess_features(df_val, scaler=scaler, encoders=encoders, fit=False)

    return X_train, y_train, X_val, y_val, scaler, encoders


def compute_class_weight(y: np.ndarray) -> float:
    """
    Compute scale_pos_weight = negatives / positives.
    Tells XGBoost how much more to penalise false negatives.
    With 10% churn rate → weight = 9.0 (minority class is 9x more important).
    """
    n_neg = (y == 0).sum()
    n_pos = (y == 1).sum()
    weight = n_neg / n_pos
    log.info(
        f"Class weights: {n_neg} negatives / {n_pos} positives → "
        f"scale_pos_weight = {weight:.2f}"
    )
    return float(weight)


class MLflowXGBCallback(xgb.callback.TrainingCallback):
    """
    Logs XGBoost eval metrics to MLflow at each boosting round.
    This gives us a real-time learning curve in the MLflow UI.

    WHY a custom callback instead of after training?
    With 500 rounds and early stopping, the final model may stop at round 230.
    Without per-round logging we'd only see the endpoint, not the learning curve.
    """

    def after_iteration(self, model, epoch: int, evals_log: dict) -> bool:
        metrics = {}
        for dataset, metric_dict in evals_log.items():
            for metric_name, values in metric_dict.items():
                metrics[f"{dataset}_{metric_name}"] = values[-1]

        if metrics:
            mlflow.log_metrics(metrics, step=epoch)
        return False  # False = don't stop training


def train(params: dict) -> dict:
    """
    Main training function. Returns final metrics dict.

    Flow:
      1. Load + preprocess data
      2. Compute class weight (auto-balancing)
      3. Setup MLflow run
      4. Train XGBoost with early stopping
      5. Evaluate on validation set + compute threshold
      6. Save artifacts: model + scaler + encoders
      7. Log to MLflow, register model
    """
    SM_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    SM_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("Loading data...")
    X_train, y_train, X_val, y_val, scaler, encoders = load_data()

    # Compute class weight if not explicitly set
    if params.get("scale_pos_weight") == "auto":
        params["scale_pos_weight"] = compute_class_weight(y_train)

    # ── MLflow setup ───────────────────────────────────────────────────────────
    tracking_uri  = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow.mlops.svc.cluster.local:5000")
    experiment    = os.environ.get("MLFLOW_EXPERIMENT_NAME", "churn-prediction-xgboost")
    run_name      = f"xgb-{os.environ.get('TRAINING_JOB_NAME', 'local')}"

    with setup_mlflow(tracking_uri, experiment, run_name=run_name, tags={"model_type": "xgboost"}):
        run_id = mlflow.active_run().info.run_id
        log.info(f"MLflow run: {run_id}")

        # ── Log hyperparameters ────────────────────────────────────────────────
        log_training_params({
            **params,
            "feature_count": X_train.shape[1],
            "train_samples": len(X_train),
            "val_samples":   len(X_val),
        })

        # ── Build DMatrix (XGBoost's optimised data format) ───────────────────
        dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=_feature_names())
        dval   = xgb.DMatrix(X_val,   label=y_val,   feature_names=_feature_names())

        # ── Train ──────────────────────────────────────────────────────────────
        xgb_params = {k: v for k, v in params.items()
                      if k not in ("n_estimators", "early_stopping_rounds")}
        xgb_params["verbosity"] = 0  # Suppress XGBoost's own logging

        log.info(f"Training XGBoost with {params['n_estimators']} rounds...")
        start = time.time()

        booster = xgb.train(
            params=xgb_params,
            dtrain=dtrain,
            num_boost_round=params["n_estimators"],
            evals=[(dtrain, "train"), (dval, "val")],
            early_stopping_rounds=params["early_stopping_rounds"],
            callbacks=[MLflowXGBCallback()],
            verbose_eval=False,
        )

        elapsed = time.time() - start
        best_round = booster.best_iteration
        log.info(f"Training complete in {elapsed:.1f}s | Best round: {best_round}")

        mlflow.log_metric("training_time_seconds", elapsed)
        mlflow.log_metric("best_boosting_round", best_round)

        # ── Optimal threshold search ───────────────────────────────────────────
        # Default 0.5 threshold is wrong for imbalanced data.
        # We choose the threshold that maximises F1 on the validation set.
        val_proba  = booster.predict(dval)
        threshold  = _find_optimal_threshold(y_val, val_proba)
        val_binary = (val_proba >= threshold).astype(int)

        # ── Evaluate ───────────────────────────────────────────────────────────
        val_metrics = compute_and_log_evaluation_metrics(
            y_val, val_proba, val_binary, split_name="val", threshold=threshold
        )

        # ── Feature importance ─────────────────────────────────────────────────
        # XGBoost provides 3 types of importance; 'gain' measures avg info gain per split
        gain_scores = booster.get_score(importance_type="gain")
        feature_names = _feature_names()
        importance_array = np.array([gain_scores.get(f, 0.0) for f in feature_names])
        log_feature_importance(feature_names, importance_array, importance_type="gain")

        # ── Save artifacts ─────────────────────────────────────────────────────
        _save_artifacts(booster, scaler, encoders, threshold, SM_MODEL_DIR)

        # ── Log artifacts to MLflow ────────────────────────────────────────────
        mlflow.log_artifacts(str(SM_MODEL_DIR), artifact_path="model")

        # ── Register model ─────────────────────────────────────────────────────
        version = register_model_to_mlflow(
            model=booster,
            model_name="churn-prediction-xgboost",
            artifact_path="model",
            run_id=run_id,
            metrics=val_metrics,
        )

        # ── SageMaker metric output (parsed by CloudWatch) ─────────────────────
        # CloudWatch looks for lines matching "metric_name: value" in stdout
        print(f"val:auc: {val_metrics['val_auc']:.6f}")
        print(f"val:precision: {val_metrics['val_precision']:.6f}")
        print(f"val:recall: {val_metrics['val_recall']:.6f}")
        print(f"val:f1: {val_metrics['val_f1']:.6f}")

        log.info(
            f"Phase 5 training complete | "
            f"AUC={val_metrics['val_auc']:.4f} | "
            f"Model version: {version}"
        )

    return val_metrics


def _feature_names() -> list[str]:
    """Return complete ordered list of feature names (numerics + encoded categoricals)."""
    return NUMERIC_FEATURES + [f"{c}_encoded" for c in CATEGORICAL_FEATURES]


def _find_optimal_threshold(y_true: np.ndarray, y_proba: np.ndarray) -> float:
    """
    Find the probability threshold that maximises F1 on the validation set.

    WHY not just use 0.5?
    With a 10% churn rate, the model naturally predicts low probabilities.
    The default 0.5 threshold will classify almost everyone as 'not churning'.
    Searching over thresholds finds the operating point that best balances
    catching churners (recall) vs precision in our interventions.
    """
    from sklearn.metrics import f1_score

    best_threshold = 0.5
    best_f1 = 0.0
    thresholds = np.arange(0.1, 0.9, 0.01)

    for t in thresholds:
        f1 = f1_score(y_true, (y_proba >= t).astype(int), zero_division=0)
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = t

    log.info(f"Optimal threshold: {best_threshold:.2f} (F1={best_f1:.4f})")
    mlflow.log_metric("optimal_threshold", best_threshold)
    return float(best_threshold)


def _save_artifacts(booster, scaler, encoders, threshold: float, output_dir: Path):
    """
    Save all artifacts needed at inference time.

    Model file: xgboost JSON format (portable across XGBoost versions)
    Scaler + encoders: pickle (sklearn objects, used in preprocessing)
    Metadata: JSON with threshold and feature list (for the inference server)
    """
    # XGBoost model — JSON is more stable than binary format across versions
    booster.save_model(str(output_dir / "model.json"))
    log.info("Saved model.json")

    # Preprocessing objects — inference container loads these to replicate training transforms
    with open(output_dir / "preprocessor.pkl", "wb") as f:
        pickle.dump({"scaler": scaler, "encoders": encoders}, f)
    log.info("Saved preprocessor.pkl")

    # Inference metadata — read by the FastAPI serving container
    metadata = {
        "model_type":          "xgboost",
        "threshold":           threshold,
        "numeric_features":    NUMERIC_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
        "feature_names":       _feature_names(),
        "target":              "is_churned",
        "training_job":        os.environ.get("TRAINING_JOB_NAME", "local"),
        "git_sha":             os.environ.get("GIT_SHA", "unknown"),
    }
    with open(output_dir / "inference_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)
    log.info("Saved inference_metadata.json")


if __name__ == "__main__":
    params = load_hyperparameters()
    metrics = train(params)
    sys.exit(0)
