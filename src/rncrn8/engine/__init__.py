"""Decision engine: how a persona turns memories of mixed provenance into a decision.

Pipeline (see docs/algorithm.md):
  1. each memory is a noisy witness about a claim
  2. witness reliability is a Beta distribution (beta.py)
  3. evidence combines in log-odds with time decay and dependence discount (evidence.py)
  4. the decision is by expected cost, not by belief alone (decision.py)
  5. outcomes update the witnesses that contributed (learning.py)
  6. personas combine into a panel with multiplicative weights (panel.py)
"""

from .beta import BetaCounts
from .calibration import Calibrator
from .decision import Decision, LossMatrix, decide
from .evidence import Contribution, Evidence, EvidenceParams, Witness, combine
from .learning import WitnessUpdate, attribute_outcome
from .metrics import Scorecard
from .panel import PanelVerdict, PanelWeights, brier_loss

__all__ = [
    "BetaCounts",
    "Calibrator",
    "Scorecard",
    "Contribution",
    "Decision",
    "Evidence",
    "EvidenceParams",
    "LossMatrix",
    "PanelVerdict",
    "PanelWeights",
    "Witness",
    "WitnessUpdate",
    "attribute_outcome",
    "brier_loss",
    "combine",
    "decide",
]
