"""Model adapters. A persona's identity lives in its files, not in which API runs it."""

from __future__ import annotations

from ..config import ModelSettings
from .base import API_KEY_PREFIXES, ModelAdapter, ModelError, ModelRequest, ModelResponse
from .dryrun import DryRunAdapter

ADAPTERS = ("claude", "dry-run")

MODEL_ENV = "RNCRN8_MODEL"
"""Overrides config.yaml's default model, so a retired model id can be replaced without editing files. A persona's own
`model`, or `--model` on the command line, still wins: those are explicit choices."""


def default_model(settings: ModelSettings) -> tuple[str, str]:
    """(the model a request without its own model goes to, where that came from)."""
    import os

    override = os.environ.get(MODEL_ENV, "").strip()
    if override:
        return override, MODEL_ENV
    return settings.model, "config.yaml"


def make_adapter(name: str | None, settings: ModelSettings, model: str | None = None) -> ModelAdapter:
    name = (name or settings.adapter).lower()
    if name in ("dry-run", "dryrun", "none"):
        return DryRunAdapter()
    if name == "claude":
        from .claude import ClaudeAdapter

        return ClaudeAdapter(model=model or default_model(settings)[0], effort=settings.effort, fallbacks=settings.fallbacks)
    raise KeyError(f"unknown adapter '{name}'; available: {', '.join(ADAPTERS)}")


__all__ = ["ADAPTERS", "API_KEY_PREFIXES", "MODEL_ENV", "DryRunAdapter", "default_model", "ModelAdapter", "ModelError", "ModelRequest", "ModelResponse", "make_adapter"]
