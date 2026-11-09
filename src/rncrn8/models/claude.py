"""Claude adapter on the Anthropic SDK.

One request per persona, structured output through output_config.format, the
system prompt marked for caching, and a server-side refusal fallback on by
default so a declined request is re-run on Anthropic's recommended substitute
instead of coming back empty.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base import API_KEY_PREFIXES, ModelError, ModelRequest, ModelResponse

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


OWN_KEY = "RNCRN8_API_KEY"
"""rncrn8's own key variable. Claude Code reads ANTHROPIC_API_KEY too, and once the person approves it, bills its own
requests to that key instead of their Claude subscription; a key in this variable reaches rncrn8 only."""


def own_key() -> str:
    return (os.environ.get(OWN_KEY) or "").strip()


WORKSPACE_VARS = ("RNCRN8_WORKSPACE_ID", "ANTHROPIC_WORKSPACE_ID")
"""Where the workspace id comes from, first match wins. A key not tied to a workspace has to name one on every call,
in the anthropic-workspace-id header; the SDK sends it from ANTHROPIC_WORKSPACE_ID only for profile and federation
credentials, never with an API key."""


def workspace_id() -> tuple[str, str] | None:
    """(the id, the variable it came from), or None."""
    for name in WORKSPACE_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    return None


def credentials_found() -> bool:
    """Whether a call will have credentials: RNCRN8_API_KEY, or what the Anthropic SDK finds on its own, namely
    ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN, a named or active `ant auth login` profile, or federation settings.
    Building the client makes no network call."""
    if own_key():
        return True
    try:
        import anthropic

        client = anthropic.Anthropic()
    except Exception:  # noqa: BLE001 - a broken profile is no credentials, and the call would say why
        return False
    return bool(client.api_key or getattr(client, "auth_token", None) or getattr(client, "credentials", None))


class ClaudeAdapter:
    name = "claude"
    supports_cache = True
    max_continuations = 5

    def __init__(
        self,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        fallbacks: bool = True,
        cache_system: bool = True,
    ):
        self._anthropic = None
        if client is None:
            import anthropic  # without RNCRN8_API_KEY: ANTHROPIC_API_KEY, then an `ant auth login` profile

            self._anthropic = anthropic
            key = own_key()
            extra: dict[str, Any] = {}
            found = workspace_id()
            if found and (key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                # with a profile the SDK fills the workspace itself, and the profile file has the last word
                extra["default_headers"] = {"anthropic-workspace-id": found[0]}
            client = anthropic.Anthropic(api_key=key, **extra) if key else anthropic.Anthropic(**extra)
        self.client = client
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self.cache_system = cache_system

    def build_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        # Each system block is a cache breakpoint. Caching is a prefix match, so the block shared by
        # every call (the review target) comes first and the persona's own block after it.
        system: list[dict[str, Any]] = [{"type": "text", "text": block} for block in request.blocks]
        if self.cache_system:
            for block in system[:4]:
                block["cache_control"] = {"type": "ephemeral"}
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": request.user}],
            "output_config": {
                "effort": request.effort or self.effort,
                "format": {"type": "json_schema", "schema": request.schema},
            },
        }
        if request.tools:
            kwargs["tools"] = request.tools
        return kwargs

    def _create(self, kwargs: dict[str, Any]):
        if self.fallbacks:
            return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def complete(self, request: ModelRequest) -> ModelResponse:
        kwargs = self.build_kwargs(request)
        sources: list[dict[str, Any]] = []
        try:
            resp = self._create(kwargs)
            # Server-side tools run in a loop on Anthropic's side; after ten iterations the turn pauses.
            # Re-sending the user message plus the assistant content resumes it; no extra user text.
            continuations = 0
            while getattr(resp, "stop_reason", None) == "pause_turn" and continuations < self.max_continuations:
                sources += self._sources(resp)
                kwargs = dict(kwargs, messages=[{"role": "user", "content": request.user}, {"role": "assistant", "content": resp.content}])
                resp = self._create(kwargs)
                continuations += 1
            sources += self._sources(resp)
        except Exception as exc:  # noqa: BLE001 - mapped below to a single reportable error
            raise ModelError(self._describe(exc, request)) from exc

        usage = getattr(resp, "usage", None)
        base = {
            "model": getattr(resp, "model", kwargs["model"]),
            "stop_reason": getattr(resp, "stop_reason", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", None),
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", None),
        }
        if base["stop_reason"] == "refusal":
            details = getattr(resp, "stop_details", None)
            return ModelResponse(
                data=None,
                text="",
                refused=True,
                refusal_category=getattr(details, "category", None),
                refusal_explanation=getattr(details, "explanation", None),
                **base,
            )
        # with server tools the final JSON is the last text block; earlier text blocks are the model's working notes
        texts = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"]
        text = texts[-1] if request.tools and texts else "".join(texts)
        if base["stop_reason"] == "max_tokens":
            raise ModelError(f"{request.label or 'request'}: output truncated at max_tokens={request.max_tokens}; raise models.max_tokens")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"{request.label or 'request'}: model returned non-JSON output: {exc}") from exc
        return ModelResponse(data=data, text=text, sources=sources, **base)

    @staticmethod
    def _sources(resp) -> list[dict[str, Any]]:
        """URLs from web_search_tool_result blocks. A failed search carries an error object instead of a list."""
        out: list[dict[str, Any]] = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) != "web_search_tool_result":
                continue
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                continue
            for item in content:
                url = getattr(item, "url", None)
                if url:
                    out.append({"url": url, "title": getattr(item, "title", None), "age": getattr(item, "page_age", None)})
        return out

    def _describe(self, exc: Exception, request: ModelRequest) -> str:
        label = request.label or "request"
        if isinstance(exc, TypeError) and "authentication method" in str(exc):
            return (
                f"{label}: no Anthropic credentials found; set RNCRN8_API_KEY, which only rncrn8 reads "
                "(PowerShell: setx RNCRN8_API_KEY \"sk-ant-api03-...\", then restart the editor; bash: export RNCRN8_API_KEY=...), "
                "or ANTHROPIC_API_KEY, or run `ant auth login`"
            )
        a = self._anthropic
        if a is not None:
            said = str(getattr(exc, "message", "") or exc)
            hint = ""
            if "workspace" in said.lower():
                hint = ("; this key is not tied to a workspace, so every call has to name one: set RNCRN8_WORKSPACE_ID, or "
                        "ANTHROPIC_WORKSPACE_ID, to the workspace id (wrkspc_...), then restart the editor or reconnect rncrn8"
                        if workspace_id() is None else f"; the workspace id sent, {workspace_id()[0]} from {workspace_id()[1]}, may be wrong")
            if isinstance(exc, a.AuthenticationError):
                if hint:
                    return f"{label}: Anthropic refused the call (401): {said}{hint}"
                return (
                    f"{label}: Anthropic rejected the API key (401): {said}. Check it starts with "
                    f"{' or '.join(API_KEY_PREFIXES)}, is copied in full and not revoked, and that its account has billing set up; "
                    "console keys are managed at https://console.anthropic.com/settings/keys"
                )
            if isinstance(exc, a.RateLimitError):
                retry = exc.response.headers.get("retry-after", "?") if getattr(exc, "response", None) is not None else "?"
                return f"{label}: rate limited (retry after {retry}s)"
            if isinstance(exc, a.NotFoundError) and "model" in said.lower():
                model = request.model or self.model
                return (f"{label}: model '{model}' was not found or is no longer offered ({said}). Set RNCRN8_MODEL, or `model` "
                        "under `models` in .rncrn8/config.yaml (or a persona's own `model`), to a current model id")
            if isinstance(exc, a.BadRequestError) and "credit balance" in said.lower():
                return (f"{label}: the Anthropic account behind this key has run out of credit ({said}). Add credits at "
                        "https://console.anthropic.com/settings/billing, then run the review again; nothing in rncrn8 needs changing")
            if isinstance(exc, a.BadRequestError):
                return f"{label}: bad request: {exc.message}{hint}"
            if isinstance(exc, a.APIStatusError):
                return f"{label}: API error {exc.status_code}: {exc.message}{hint}"
            if isinstance(exc, a.APIConnectionError):
                return f"{label}: could not reach the Anthropic API"
        return f"{label}: {type(exc).__name__}: {exc}"
