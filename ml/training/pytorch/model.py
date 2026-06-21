"""
PyTorch MLP for churn prediction.

WHY a neural network in addition to XGBoost?
  XGBoost cannot learn arbitrary feature interactions automatically.
  An MLP can learn non-linear combinations of the 25 features via hidden layers.
  In practice, for small tabular datasets XGBoost usually wins, but the MLP is
  useful to:
  1. Verify XGBoost is not underfitting
  2. As an ensemble member (average XGB + MLP probabilities)
  3. As a fast baseline for adding sequence/embedding layers later
     (e.g., if we want to feed raw event sequences instead of aggregated features)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class ChurnMLP(nn.Module):
    """
    Feed-forward MLP with:
    - Batch normalisation (stabilises training, reduces sensitivity to LR)
    - Dropout (reduces overfitting on small datasets)
    - Residual connections in middle layers (mitigates vanishing gradients)

    Input: [batch_size, n_features]   (float32)
    Output: [batch_size, 1]           (logit, not probability — use sigmoid at inference)
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dims: list[int] = (256, 128, 64),
        dropout_rate: float = 0.3,
    ):
        super().__init__()

        self.input_bn = nn.BatchNorm1d(input_dim)

        layers = []
        in_dim = input_dim
        for i, out_dim in enumerate(hidden_dims):
            layers.append(nn.Linear(in_dim, out_dim))
            layers.append(nn.BatchNorm1d(out_dim))
            layers.append(nn.ReLU())
            layers.append(nn.Dropout(dropout_rate))
            in_dim = out_dim

        self.hidden = nn.Sequential(*layers)
        self.output = nn.Linear(in_dim, 1)

        # Xavier initialisation — prevents exploding/vanishing gradients on first pass
        self._init_weights()

    def _init_weights(self):
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.input_bn(x)
        x = self.hidden(x)
        return self.output(x)  # Raw logit — caller applies sigmoid


class FocalLoss(nn.Module):
    """
    Focal loss for imbalanced classification.

    WHY focal loss instead of standard BCE with class weights?
    Standard weighted BCE down-weights negatives uniformly.
    Focal loss additionally down-weights EASY positives — examples the model
    already classifies correctly with high confidence. This forces the model
    to focus learning on hard/ambiguous examples near the decision boundary,
    which is where churn prediction is actually difficult.

    gamma=2 is the standard starting point from the original paper.
    alpha=class weight (computed from training data).
    """

    def __init__(self, alpha: float = 1.0, gamma: float = 2.0):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma

    def forward(self, logits: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        bce = F.binary_cross_entropy_with_logits(
            logits, targets.float(), reduction="none"
        )
        pt = torch.exp(-bce)  # Probability of the correct class
        focal_weight = self.alpha * (1 - pt) ** self.gamma
        return (focal_weight * bce).mean()
