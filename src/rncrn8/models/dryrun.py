"""No API. Records every request so you can read exactly what each persona would receive."""

from __future__ import annotations

import json
from typing import Any, Callable

from .base import ModelRequest, ModelResponse

Script = Callable[[ModelRequest], dict[str, Any]]


class DryRunAdapter:
    name = "dry-run"
    supports_cache = False

    def __init__(self, script: Script | None = None):
        self.requests: list[ModelRequest] = []
        self.script = script

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.script is not None:
            data = self.script(request)
        else:
            data = {"understood": "(dry run: no model was called)", "findings": [], "notes": "dry run"}
        return ModelResponse(data=data, text=json.dumps(data), model="dry-run", stop_reason="end_turn")
