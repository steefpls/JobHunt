"""The HTTP model client: provider details, spending caps, rate limits."""
import json

import httpx
import pytest

from jobhunt import llm, settings
from jobhunt.llm import BudgetExceeded, LLMClient, LLMError, Meter, RateLimited

from conftest import API_KEY


def _ok(content="{\"a\": 1}", usage=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": msg}], "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20}})


class Recorder:
    def __init__(self, responder=None):
        self.requests = []
        self.responder = responder or (lambda req: _ok())
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        return self.responder(request)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_opencode_go_request_shape():
    s = settings.load()  # defaults: OpenCode Go, deepseek-v4.1-flash
    assert s["provider"] == "opencode_go"
    rec = Recorder()
    meter = Meter.from_settings(s)
    c = LLMClient(s, API_KEY, meter=meter, transport=rec.transport, session_prefix="run7")
    data, resp = c.chat_json(stage="filter", system="sys", user="usr", job_id=3)
    assert data == {"a": 1}
    req = rec.requests[0]
    assert str(req.url) == "https://opencode.ai/zen/go/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {API_KEY}"
    assert req.headers["user-agent"].startswith("JobHunt/")
    body = json.loads(req.content)
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] == "deepseek-v4.1-flash"
    assert API_KEY not in req.content.decode()  # the key is a header, never the body
    # stable session id per job + stage
    sid = req.headers["x-opencode-session"]
    assert sid == c.session_id(3, "filter") and sid != c.session_id(3, "tailor") and sid != c.session_id(4, "filter")
    # flat subscription: nothing is billed, but the cost is shown at the model's API price
    # (OpenCode's "deepseek-v4.1-flash" is priced as OpenRouter's "deepseek/deepseek-v4.1-flash")
    assert resp.cost == pytest.approx((100 * 0.0441 + 20 * 0.63) / 1e6)
    assert meter.snapshot()["tokens_in"] == 100


def test_openrouter_cost_and_flags():
    s = settings.load()
    s.update(provider="openrouter", models=dict(settings.PROVIDER_DEFAULT_MODELS["openrouter"]))
    rec = Recorder(lambda r: _ok(usage={"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.0123}))
    resp = LLMClient(s, API_KEY, transport=rec.transport).chat(stage="tailor", system="s", user="u")
    body = json.loads(rec.requests[0].content)
    assert str(rec.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert body["usage"] == {"include": True} and body["reasoning"] == {"enabled": False}
    assert resp.cost == pytest.approx(0.0123)


def test_cost_from_price_table_when_provider_does_not_say():
    s = settings.load()
    s.update(provider="openrouter", models={"tailor": "deepseek/deepseek-v4.1-flash"})
    rec = Recorder(lambda r: _ok(usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}))
    resp = LLMClient(s, API_KEY, transport=rec.transport).chat(stage="tailor", system="s", user="u")
    assert resp.cost == pytest.approx(0.0441 + 0.63)


def test_token_cap_stops_before_the_call():
    s = settings.load()
    meter = Meter(0, 0, run_token_cap=500, job_token_cap=0, subscription=True)
    rec = Recorder()
    c = LLMClient(s, API_KEY, meter=meter, transport=rec.transport)
    with pytest.raises(BudgetExceeded) as e:
        c.chat(stage="filter", system="s" * 3000, user="u", max_tokens=400)
    assert e.value.unit == "tokens"
    assert rec.requests == []  # never sent


def test_usd_job_cap_for_pay_per_use():
    meter = Meter(run_cap=1.0, job_cap=0.01)
    meter.add(5, 0.009, 10, 10)
    meter.check(6, 0.005)  # other job is fine
    with pytest.raises(BudgetExceeded):
        meter.check(5, 0.005)
    meter.move_job(5, 9)
    assert meter.job_costs == {9: pytest.approx(0.009)}


def test_subscription_ignores_usd_caps():
    meter = Meter(run_cap=0.01, job_cap=0.01, subscription=True)
    meter.check(1, 5.0)  # no exception


def test_429_becomes_rate_limited():
    s = settings.load()
    rec = Recorder(lambda r: httpx.Response(429, json={"error": "slow down"}))
    with pytest.raises(RateLimited):
        LLMClient(s, API_KEY, transport=rec.transport).chat(stage="filter", system="s", user="u")
    assert len(rec.requests) == 4  # retried with back-off first


def test_bad_key_message_has_no_key():
    s = settings.load()
    rec = Recorder(lambda r: httpx.Response(401, text=f"bad key {API_KEY}"))
    with pytest.raises(LLMError) as e:
        LLMClient(s, API_KEY, transport=rec.transport).chat(stage="filter", system="s", user="u")
    assert API_KEY not in str(e.value)
    assert API_KEY not in repr(LLMClient(s, API_KEY))


def test_missing_key_is_a_clear_error():
    with pytest.raises(LLMError, match="No API key"):
        LLMClient(settings.load(), "")


def test_tool_calls_are_parsed():
    s = settings.load()
    calls = [{"id": "c1", "type": "function", "function": {"name": "click", "arguments": "{\"element\": \"e3\"}"}}]
    rec = Recorder(lambda r: _ok(content="", tool_calls=calls))
    resp = LLMClient(s, API_KEY, transport=rec.transport).chat(
        stage="apply", system="s", user="u", tools=[{"name": "click", "description": "d", "parameters": {"type": "object"}}])
    assert resp.tool_calls[0].name == "click" and resp.tool_calls[0].args == {"element": "e3"}
    body = json.loads(rec.requests[0].content)
    assert body["tools"][0]["function"]["name"] == "click"


def test_anthropic_provider():
    import httpx2

    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "{\"decision\": \"apply\"}"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 9}})

    s = settings.load()
    s.update(provider="anthropic", models=dict(settings.PROVIDER_DEFAULT_MODELS["anthropic"]))
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    data, resp = LLMClient(s, API_KEY, anthropic_http_client=client).chat_json(stage="filter", system="s", user="u")
    assert data == {"decision": "apply"}
    assert (resp.tokens_in, resp.tokens_out) == (120, 9)
    assert seen[0].headers["x-api-key"] == API_KEY
    assert API_KEY not in seen[0].content.decode()


@pytest.mark.parametrize("text", [
    '{"a": 1}',
    'Sure! ```json\n{"a": 1}\n``` hope that helps',
    'Here you go: {"a": 1} and some trailing words with a } brace',
])
def test_extract_json(text):
    assert llm.extract_json(text) == {"a": 1}


def test_extract_json_failure():
    with pytest.raises(ValueError):
        llm.extract_json("no json here")
