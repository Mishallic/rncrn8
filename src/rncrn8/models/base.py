from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


API_KEY_PREFIXES = ("sk-ant-api", "sk-ant-usr")
"""How accepted Anthropic API keys start. Read by `doctor` and by the adapter's message for a rejected key."""


class ModelError(RuntimeError):
    """A call failed in a way the caller should report, not retry blindly."""


@dataclass
class ModelRequest:
    system: str
    user: str
    schema: dict[str, Any]
    max_tokens: int = 16000
    model: str | None = None
    effort: str | None = None
    label: str = ""
    system_blocks: list[str] | None = None
    """Ordered system blocks, each a cache breakpoint. The first block is the one most calls share
    (the review target); the persona's own block follows. When None, `system` is the single block."""
    tools: list[dict[str, Any]] | None = None
    """Server-side tool definitions (e.g. web search) passed through to the provider."""

    @property
    def blocks(self) -> list[str]:
        return list(self.system_blocks) if self.system_blocks else [self.system]


@dataclass
class ModelResponse:
    data: dict[str, Any] | None
    text: str
    model: str
    stop_reason: str | None = None
    refused: bool = False
    refusal_category: str | None = None
    refusal_explanation: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    """URLs the provider's search tool visited, when a request carried tools."""


class ModelAdapter(Protocol):
    name: str
    supports_cache: bool

    def complete(self, request: ModelRequest) -> ModelResponse: ...
