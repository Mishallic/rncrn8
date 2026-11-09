"""All tunable constants of the engine, in one YAML file per workspace.

A change to any value is accepted only if it improves held-out replay
(deliverable 4). Until then these are defaults with stated reasoning.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .engine.beta import BetaCounts
from .engine.decision import LossMatrix
from .engine.evidence import EvidenceParams


class SourceType(str, Enum):
    """Where a memory came from. Carried on every entry; weighted differently by every persona."""

    PROJECT = "project"      # read from the repo or folder itself
    HISTORY = "history"      # git log, pull request comments, issues
    INTERNET = "internet"    # searched or imported from the web
    HUMAN = "human"          # a person told us: questionnaire, call notes, chat
    OUTCOME = "outcome"      # something that actually happened and was recorded
    INFERENCE = "inference"  # the persona's own reasoning from a target


class SourcePrior(BaseModel):
    """Prior reliability and time behaviour of one source class."""

    model_config = {"frozen": True}

    a: float = Field(gt=0)
    b: float = Field(gt=0)
    halflife_days: float | None = Field(default=None, description="evidence weight halves every this many days; null = no decay")
    review_after_days: float | None = Field(default=None, description="past this age the memory is stale until re-checked; null = never")

    @property
    def counts(self) -> BetaCounts:
        return BetaCounts(a=self.a, b=self.b)


def default_source_priors() -> dict[SourceType, SourcePrior]:
    return {
        # Recorded facts about what happened. Strongest, do not decay on their own.
        SourceType.OUTCOME: SourcePrior(a=9, b=1),
        # A person with context said it. Strong; people and markets drift over years.
        SourceType.HUMAN: SourcePrior(a=8, b=2, halflife_days=3 * 365),
        # Read from the project itself. Strong but re-read often, so no halflife here.
        SourceType.PROJECT: SourcePrior(a=7, b=3),
        # Git and tracker history: real, but old decisions get revisited.
        SourceType.HISTORY: SourcePrior(a=6, b=4, halflife_days=2 * 365),
        # The persona's own inference: medium, decays as the target changes.
        SourceType.INFERENCE: SourcePrior(a=5, b=5, halflife_days=365),
        # The web: plentiful and weak on arrival; earns its way up through outcomes.
        SourceType.INTERNET: SourcePrior(a=4, b=3, halflife_days=180, review_after_days=180),
    }


class ModelSettings(BaseModel):
    """Which model runs the personas. The persona's own `model` field overrides `model` here."""

    adapter: str = Field(default="claude", description="claude | dry-run")
    model: str = Field(default="claude-opus-5-5")
    effort: str = Field(default="high", description="low | medium | high | xhigh | max")
    max_tokens: int = Field(default=16000, gt=0)
    fallbacks: bool = Field(default=True, description="server-side refusal fallback on the Claude API")


class ReviewSettings(BaseModel):
    max_findings_per_persona: int = Field(default=7, gt=0)
    memory_limit: int = Field(default=40, gt=0, description="most relevant memories shown to a persona per review")
    memory_char_budget: int = Field(default=12000, gt=0)
    confidence_floor: float = Field(default=0.05, gt=0, lt=1)
    confidence_ceiling: float = Field(default=0.95, gt=0, lt=1)
    parallel: int = Field(default=6, gt=0, description="persona calls in flight at once")
    debate: bool = Field(default=True, description="run the debate round when two or more personas reviewed")
    warm_cache: bool = Field(default=True, description="run the first persona alone so the shared target block is cached before the others start")
    max_packet_chars: int = Field(default=400_000, gt=0, description="refuse to review a target larger than this without --force; roughly 100k tokens per persona call. Context shown beside it, such as unchanged code that uses what a change modifies, does not count")
    update_summaries: bool = Field(default=True, description="each persona rewrites its summary.md after a review")
    summary_max_words: int = Field(default=300, gt=0)
    summary_max_chars: int = Field(default=8000, gt=0, description="hard cap on a rewritten summary.md, which every future prompt loads")
    panels: dict[str, list[str]] = Field(
        default_factory=dict,
        description="default panel per target kind (code, document, idea, video) for a review that names none; a kind not listed gets every persona that applies",
    )


class CalibrationSettings(BaseModel):
    """Map a persona's stated confidence to its observed hit rate (isotonic, shrunk to identity)."""

    enabled: bool = True
    prior_strength: float = Field(default=5.0, gt=0, description="pseudo-points of identity before the record takes over")
    min_points: int = Field(default=3, ge=1, description="below this many outcomes the persona is taken at its word")


class PanelSettings(BaseModel):
    """Hedge weights per persona from the Brier loss of its readings, applied to reading witnesses."""

    apply_weights: bool = True
    influence: float = Field(default=0.5, ge=0, le=1, description="0 ignores the weights; 1 applies the full weight ratio")


class SourceSettings(BaseModel):
    """Memory sources: how much one ingest may record."""

    max_facts: int = Field(default=25, gt=0, description="facts one ingest or research run may record")
    research_budget: int = Field(default=5, gt=0, description="web searches one research run may make")
    history_commits: int = Field(default=200, gt=0)


class OutcomeSettings(BaseModel):
    implicit_learning_rate: float = Field(default=0.5, gt=0, description="the amount recorded with a self-check outcome; such outcomes teach nothing (see outcome_source), so it moves no trust")
    commit_learning_rate: float = Field(default=0.3, gt=0, description="the amount recorded with a commit closure; such outcomes teach nothing (see outcome_source), so it moves no trust")
    followup_max_lines: int = Field(default=150, gt=0, description="a finding whose cited lines span more than this is never closed by a commit: a change somewhere in a whole file says nothing about one finding")



class EngineConfig(BaseModel):
    prior_belief: float = Field(default=0.5, gt=0, lt=1, description="belief in a claim before any witness speaks")
    max_witness_weight: float = Field(default=4.0, gt=0)
    allow_negative_reliability: bool = False
    dependence_exponent: float = Field(default=0.5, ge=0, le=1)
    stale_factor: float = Field(default=0.3, ge=0, le=1)
    max_cluster_weight: float = Field(default=8.0, gt=0, description="cap on the total log-odds one dependence cluster may add")
    witness_learning_rate: float = Field(default=1.0, gt=0, description="total pseudo-count one outcome distributes over its witnesses")
    panel_learning_rate: float = Field(default=0.5, gt=0, description="eta of the Hedge update over personas")
    default_loss: LossMatrix = Field(default_factory=LossMatrix)
    source_priors: dict[SourceType, SourcePrior] = Field(default_factory=default_source_priors)
    models: ModelSettings = Field(default_factory=ModelSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings)
    panel: PanelSettings = Field(default_factory=PanelSettings)
    outcomes: OutcomeSettings = Field(default_factory=OutcomeSettings)
    sources: SourceSettings = Field(default_factory=SourceSettings)

    def evidence_params(self) -> EvidenceParams:
        return EvidenceParams(
            max_witness_weight=self.max_witness_weight,
            allow_negative_reliability=self.allow_negative_reliability,
            dependence_exponent=self.dependence_exponent,
            stale_factor=self.stale_factor,
            max_cluster_weight=self.max_cluster_weight,
        )

    def prior_for(self, source: SourceType) -> SourcePrior:
        return self.source_priors.get(source) or default_source_priors()[source]

    # --- persistence -----------------------------------------------------------------

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json")
        header = (
            "# rncrn8 engine configuration. Every value here is a tunable.\n"
            "# Change one, replay history (deliverable 4), keep it only if the scores improve.\n"
        )
        return header + yaml.safe_dump(data, sort_keys=False)

    @classmethod
    def from_yaml(cls, text: str) -> "EngineConfig":
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        path.write_text(self.to_yaml(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "EngineConfig":
        if not path.exists():
            return cls()
        return cls.from_yaml(path.read_text(encoding="utf-8"))
