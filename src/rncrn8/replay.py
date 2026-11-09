"""Replay harness: re-score closed decisions under a candidate config and compare.

Decisions are ordered by time. Calibrators and panel weights are fit on the older
part and scored on the newer part, so nothing is tuned on what it is tested on.
Only decisions that stored raw witness fields can be replayed; older ones count
toward the "as recorded" baseline only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import EngineConfig, SourceType
from .engine import Scorecard, Witness, combine
from .memory import MemoryStore, Workspace
from .memory.store import DecisionRecord
from .track import PanelState, fit_calibrators

_SOURCES = {s.value for s in SourceType}
NO_LEARNING = "no-learning"


def rebuild_witnesses(rec: DecisionRecord, config: EngineConfig, calibrators: dict, panel: PanelState | None) -> list[Witness] | None:
    witnesses = []
    for c in rec.contributions:
        if "age_days" not in c:
            return None
        persona = c.get("persona", rec.persona)
        role = c.get("role", "memory")
        if role == "reading":
            stated = float(c.get("stated", c["reliability"]))
            cal = calibrators.get(persona) if config.calibration.enabled else None
            reliability = cal(stated) if cal else stated
            scale = panel.scale_for(persona, config.panel.influence) if (panel is not None and config.panel.apply_weights) else 1.0
            halflife = config.prior_for(SourceType.INFERENCE).halflife_days
        else:
            reliability = float(c["reliability"])
            scale = 1.0
            source = c.get("source")
            halflife = config.prior_for(SourceType(source)).halflife_days if source in _SOURCES else c.get("halflife_days")
        witnesses.append(
            Witness(
                ref=c["ref"],
                reliability=reliability,
                supports=bool(c["supports"]),
                age_days=float(c.get("age_days", 0.0)),
                halflife_days=halflife,
                cluster=c.get("cluster"),
                stale=bool(c.get("stale", False)),
                scale=scale,
            )
        )
    return witnesses


@dataclass
class ReplayResult:
    label: str
    n_train: int
    n_test: int
    n_skipped: int
    recorded: Scorecard
    replayed: Scorecard
    preds: list[float] = field(default_factory=list)
    outcomes: list[bool] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "n_train": self.n_train,
            "n_test": self.n_test,
            "n_skipped": self.n_skipped,
            "as_recorded": self.recorded.as_dict(),
            "replayed": self.replayed.as_dict(),
        }


@dataclass
class ReplayReport:
    results: list[ReplayResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {"results": [r.as_dict() for r in self.results], "notes": self.notes}
        baseline = next((r for r in self.results if r.label == NO_LEARNING), None)
        if baseline is not None:
            out["learning_vs_none"] = compare_learning(self.results[0], baseline).as_dict()
        return out


def _split(decisions: list[DecisionRecord], holdout: float) -> tuple[list[DecisionRecord], list[DecisionRecord], list[str]]:
    notes: list[str] = []
    n = len(decisions)
    if n < 5:
        notes.append(f"only {n} closed decisions: no train/test split, calibration and weights fit on nothing")
        return [], decisions, notes
    n_test = max(1, round(n * holdout))
    return decisions[: n - n_test], decisions[n - n_test :], notes


def run_once(label: str, config: EngineConfig, train: list[DecisionRecord], test: list[DecisionRecord], learn: bool = True) -> ReplayResult:
    """Fit on `train`, score `test`. With learn=False nothing is fitted: no calibration and equal panel
    weights, the baseline the learning has to beat."""
    calibrators = fit_calibrators(train, config.calibration) if learn and config.calibration.enabled else {}
    from .track import counts_for_learning

    panel = PanelState()
    for rec in train if learn else []:
        if counts_for_learning(rec):
            panel.update_from_decision(rec, rec.outcome, config.panel_learning_rate)
    preds, recorded, outcomes = [], [], []
    skipped = 0
    for rec in test:
        witnesses = rebuild_witnesses(rec, config, calibrators, panel)
        if witnesses is None:
            skipped += 1
            continue
        preds.append(combine(witnesses, config.prior_belief, config.evidence_params()).belief)
        recorded.append(rec.belief)
        outcomes.append(bool(rec.outcome))
    return ReplayResult(label=label, n_train=len(train), n_test=len(test), n_skipped=skipped, recorded=Scorecard.of(recorded, outcomes),
                        replayed=Scorecard.of(preds, outcomes), preds=preds, outcomes=outcomes)


MIN_COMPARED = 10
"""Below this many held-out decisions a comparison says it cannot tell, rather than guess."""


@dataclass
class Comparison:
    n: int
    difference: float | None = None
    low: float | None = None
    high: float | None = None
    verdict: str = ""

    def as_dict(self) -> dict:
        return {"n": self.n, "brier_difference": self.difference, "interval_95": [self.low, self.high] if self.low is not None else None, "verdict": self.verdict}


def compare_learning(learned: ReplayResult, baseline: ReplayResult, resamples: int = 2000) -> Comparison:
    """Whether `learned` beat `baseline` on the same held-out decisions: the mean difference in Brier score per
    decision (negative is better), with a 95% interval from a paired bootstrap. The verdict is 'helped' or 'hurt'
    only when the interval leaves out zero."""
    import random

    n = len(learned.preds)
    if n != len(baseline.preds) or learned.outcomes != baseline.outcomes:
        return Comparison(n=n, verdict="not comparable: the two runs scored different decisions")
    if n < MIN_COMPARED:
        return Comparison(n=n, verdict=f"too few held-out decisions ({n}) to say; needs {MIN_COMPARED}, so record more outcomes")
    if all(abs(a - b) < 1e-12 for a, b in zip(learned.preds, baseline.preds)):
        return Comparison(n=n, difference=0.0, verdict="nothing was learned from the older outcomes (too few per persona to fit), so there is nothing to compare yet")
    diffs = [(a - y) ** 2 - (b - y) ** 2 for a, b, y in zip(learned.preds, baseline.preds, learned.outcomes)]
    mean = sum(diffs) / n
    rng = random.Random(0)  # fixed, so the same data always gives the same interval
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    low, high = means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]
    if high < 0:
        verdict = "helped"
    elif low > 0:
        verdict = "hurt"
    else:
        verdict = "no clear difference: the interval includes zero"
    return Comparison(n=n, difference=mean, low=low, high=high, verdict=verdict)


def replay(ws: Workspace, candidate: EngineConfig | None = None, holdout: float = 0.2, without_learning: bool = False) -> ReplayReport:
    from .track import counts_for_learning

    # the held-out test set obeys the same rule as training: an outcome no person stands behind can only
    # say yes, so scoring a config on it would reward whatever agrees with confirm-only labels
    decisions = sorted([d for d in MemoryStore(ws).load_decisions() if counts_for_learning(d)], key=lambda d: d.created_at)
    report = ReplayReport()
    if not decisions:
        report.notes.append("no closed decisions; record outcomes first")
        return report
    train, test, notes = _split(decisions, holdout)
    report.notes += notes
    report.results.append(run_once("current", ws.load_config(), train, test))
    if candidate is not None:
        report.results.append(run_once("candidate", candidate, train, test))
    if without_learning:
        report.results.append(run_once(NO_LEARNING, ws.load_config(), train, test, learn=False))
    skipped = report.results[0].n_skipped
    if skipped:
        report.notes.append(f"{skipped} test decisions predate raw witness fields and were scored as recorded only")
    return report
