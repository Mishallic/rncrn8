import json

import pytest

from rncrn8.models import ModelError, ModelRequest
from rncrn8.models.claude import FALLBACK_BETA, ClaudeAdapter


class Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class Usage:
    input_tokens = 100
    output_tokens = 20
    cache_read_input_tokens = 80


class Details:
    category = "cyber"
    explanation = "declined"


class Resp:
    def __init__(self, text="", stop_reason="end_turn", details=None):
        self.content = [Block(text)] if text else []
        self.stop_reason = stop_reason
        self.model = "claude-opus-5-5"
        self.usage = Usage()
        self.stop_details = details


class Endpoint:
    def __init__(self, resp):
        self.resp = resp
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


class FakeClient:
    def __init__(self, resp):
        self.messages = Endpoint(resp)
        self.beta = type("Beta", (), {})()
        self.beta.messages = Endpoint(resp)


def req(**kw):
    base = dict(system="SYS", user="USER", schema={"type": "object"}, label="critic")
    base.update(kw)
    return ModelRequest(**base)


def test_request_shape_with_fallbacks():
    client = FakeClient(Resp(json.dumps({"ok": True})))
    adapter = ClaudeAdapter(client=client, model="claude-opus-5-5", effort="high")
    out = adapter.complete(req())
    k = client.beta.messages.kwargs
    assert client.messages.kwargs is None  # fallbacks go through the beta endpoint
    assert k["betas"] == [FALLBACK_BETA] and k["fallbacks"] == "default"
    assert k["model"] == "claude-opus-5-5" and k["max_tokens"] == 16000
    assert k["system"][0]["text"] == "SYS" and k["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert k["messages"] == [{"role": "user", "content": "USER"}]
    assert k["output_config"]["effort"] == "high"
    assert k["output_config"]["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert "thinking" not in k  # always on for this model family; effort is the control
    assert out.data == {"ok": True} and out.input_tokens == 100 and out.cache_read_tokens == 80


def test_without_fallbacks_uses_plain_endpoint_and_overrides():
    client = FakeClient(Resp("{}"))
    adapter = ClaudeAdapter(client=client, fallbacks=False, cache_system=False)
    adapter.complete(req(model="claude-sonnet-5-5", effort="low", max_tokens=2000))
    k = client.messages.kwargs
    assert client.beta.messages.kwargs is None
    assert k["model"] == "claude-sonnet-5-5" and k["output_config"]["effort"] == "low" and k["max_tokens"] == 2000
    assert "cache_control" not in k["system"][0]


def test_refusal_is_reported_not_raised():
    client = FakeClient(Resp("", stop_reason="refusal", details=Details()))
    out = ClaudeAdapter(client=client).complete(req())
    assert out.refused and out.data is None
    assert out.refusal_category == "cyber" and out.refusal_explanation == "declined"


def test_truncation_and_bad_json_are_errors():
    with pytest.raises(ModelError, match="max_tokens"):
        ClaudeAdapter(client=FakeClient(Resp("{\"a\":", stop_reason="max_tokens"))).complete(req())
    with pytest.raises(ModelError, match="non-JSON"):
        ClaudeAdapter(client=FakeClient(Resp("not json"))).complete(req())


def test_api_exception_becomes_model_error_with_label():
    with pytest.raises(ModelError, match="critic: RuntimeError: boom"):
        ClaudeAdapter(client=FakeClient(RuntimeError("boom"))).complete(req())


def test_missing_credentials_message_is_specific():
    err = TypeError("Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set.")
    with pytest.raises(ModelError, match="no Anthropic credentials found"):
        ClaudeAdapter(client=FakeClient(err)).complete(req())


def test_rejected_key_message_is_specific():
    import anthropic
    import httpx2 as httpx

    adapter = ClaudeAdapter(client=FakeClient(RuntimeError("placeholder")))
    adapter._anthropic = anthropic
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    adapter.client.beta.messages.resp = anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
    with pytest.raises(ModelError, match="rejected the API key.*starts with sk-ant-api or sk-ant-usr"):
        adapter.complete(req())


def test_system_blocks_each_get_a_cache_breakpoint_and_tools_pass_through():
    client = FakeClient(Resp(json.dumps({"facts": []})))
    adapter = ClaudeAdapter(client=client)
    adapter.complete(req(system_blocks=["TARGET", "PERSONA"], tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}]))
    k = client.beta.messages.kwargs
    assert [b["text"] for b in k["system"]] == ["TARGET", "PERSONA"]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in k["system"])
    assert k["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}]


class PausingEndpoint:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            r = Resp("", stop_reason="pause_turn")
            hit = type("Hit", (), {"url": "https://a.example/p", "title": "A", "page_age": "3 days"})()
            tool_block = type("TB", (), {"type": "web_search_tool_result", "content": [hit]})()
            r.content = [Block("working..."), tool_block]
            return r
        r = Resp("", stop_reason="end_turn")
        r.content = [Block("notes"), Block(json.dumps({"facts": [], "notes": "done"}))]
        return r


def test_pause_turn_is_resumed_and_sources_collected():
    client = FakeClient(Resp("{}"))
    client.beta.messages = PausingEndpoint()
    out = ClaudeAdapter(client=client).complete(req(tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 1}]))
    assert out.data == {"facts": [], "notes": "done"}  # the last text block is the JSON
    assert out.sources == [{"url": "https://a.example/p", "title": "A", "age": "3 days"}]
    second = client.beta.messages.calls[1]
    assert second["messages"][0] == {"role": "user", "content": "USER"} and second["messages"][1]["role"] == "assistant"


def test_a_workspace_id_is_sent_with_an_api_key_and_rncrn8s_own_comes_first(monkeypatch):
    for name in ("RNCRN8_WORKSPACE_ID", "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RNCRN8_API_KEY", "sk-ant-usr-test-only")
    assert "anthropic-workspace-id" not in ClaudeAdapter().client.default_headers  # most keys need none
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_from_anthropic_var")
    assert ClaudeAdapter().client.default_headers["anthropic-workspace-id"] == "wrkspc_from_anthropic_var"
    monkeypatch.setenv("RNCRN8_WORKSPACE_ID", "wrkspc_for_rncrn8")
    assert ClaudeAdapter().client.default_headers["anthropic-workspace-id"] == "wrkspc_for_rncrn8"


def test_a_refusal_about_the_workspace_keeps_the_apis_words_and_says_what_to_set(monkeypatch):
    import anthropic
    import httpx2 as httpx

    for name in ("RNCRN8_WORKSPACE_ID", "ANTHROPIC_WORKSPACE_ID"):
        monkeypatch.delenv(name, raising=False)
    adapter = ClaudeAdapter(client=FakeClient(RuntimeError("placeholder")))
    adapter._anthropic = anthropic
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    adapter.client.beta.messages.resp = anthropic.AuthenticationError("this API key requires the anthropic-workspace-id header", response=response, body=None)
    with pytest.raises(ModelError) as info:
        adapter.complete(req())
    msg = str(info.value)
    assert "requires the anthropic-workspace-id header" in msg and "set RNCRN8_WORKSPACE_ID, or ANTHROPIC_WORKSPACE_ID" in msg
    assert "rejected the API key" not in msg  # the key may be fine; the call named no workspace


def test_rncrn8_model_overrides_the_configured_model(monkeypatch):
    from rncrn8.config import ModelSettings
    from rncrn8.models import default_model, make_adapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "x" * 20)
    monkeypatch.setenv("RNCRN8_MODEL", "claude-test-model")
    assert default_model(ModelSettings()) == ("claude-test-model", "RNCRN8_MODEL")
    assert make_adapter("claude", ModelSettings()).model == "claude-test-model"
    assert make_adapter("claude", ModelSettings(), model="persona-model").model == "persona-model"
    monkeypatch.delenv("RNCRN8_MODEL")
    assert default_model(ModelSettings()) == (ModelSettings().model, "config.yaml")
    assert make_adapter("claude", ModelSettings()).model == ModelSettings().model


def test_doctor_live_check_uses_the_override(monkeypatch):
    from rncrn8 import doctor
    from rncrn8.config import ModelSettings
    from rncrn8.models import ModelResponse

    seen = {}

    class Fake:
        name = "claude"

        def complete(self, request):
            seen["model"] = request.model
            return ModelResponse(data={"ok": True}, text="{}", model=request.model)

    monkeypatch.setattr(doctor, "make_adapter", lambda *a, **k: Fake())
    monkeypatch.setenv("RNCRN8_MODEL", "claude-test-model")
    check = doctor._live_check(ModelSettings(model="claude-retired"), "claude")
    assert seen["model"] == "claude-test-model" and check.level == "ok"


def _api_error(cls, status, message):
    import anthropic
    import httpx2 as httpx

    adapter = ClaudeAdapter(client=FakeClient(RuntimeError("placeholder")))
    adapter._anthropic = anthropic
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    adapter.client.beta.messages.resp = getattr(anthropic, cls)(message, response=response, body=None)
    return adapter


def test_a_missing_model_names_the_override():
    adapter = _api_error("NotFoundError", 404, "model: claude-retired")
    with pytest.raises(ModelError, match="was not found or is no longer offered.*RNCRN8_MODEL"):
        adapter.complete(req())


def test_an_unrelated_bad_request_is_not_reported_as_a_missing_model():
    adapter = _api_error("BadRequestError", 400, "web_search is not supported for this model")
    with pytest.raises(ModelError, match="bad request: web_search is not supported") as info:
        adapter.complete(req())
    assert "no longer offered" not in str(info.value)


def test_an_empty_credit_balance_says_what_to_do():
    adapter = _api_error("BadRequestError", 400, "Your credit balance is too low to access the Anthropic API.")
    with pytest.raises(ModelError, match="run out of credit.*console.anthropic.com/settings/billing"):
        adapter.complete(req())


def test_review_requests_leave_the_model_to_the_adapter(ws):
    from rncrn8.models import DryRunAdapter
    from rncrn8.packet import packet_from_text
    from rncrn8.review import run_review

    adapter = DryRunAdapter()
    run_review(ws, packet_from_text("# Plan\n\nShip it.\n", "plan"), adapter, panel=["critic"], debate=False, summaries=False)
    # no persona model and no --model: the request carries none, so the adapter's default (RNCRN8_MODEL included) applies
    assert adapter.requests and all(r.model is None for r in adapter.requests)
