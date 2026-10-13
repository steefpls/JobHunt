"""Talking to the language model over HTTP, with a hard spending meter.

Every model call in the app is a single, fresh request: one system prompt and
one user message (plus tool definitions for the apply agent). No long chats,
so the cost of each call is small and predictable.

Providers
  opencode_go        https://opencode.ai/zen/go/v1 (OpenAI-style; flat subscription, so
                     no per-call price: token caps instead of USD caps)
  openrouter         https://openrouter.ai/api/v1  (OpenAI-style API, reports cost)
  openai_compatible  any other OpenAI-style endpoint (base URL from settings)
  anthropic          the Anthropic Messages API, through the official SDK

The API key comes from Windows Credential Manager and is only ever placed in an
HTTP header. It is never logged and never part of a prompt.
"""
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field

import httpx

from . import __version__, paths
from .settings import SUBSCRIPTION_PROVIDERS

OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENCODE_GO_URL = "https://opencode.ai/zen/go/v1"
USER_AGENT = f"JobHunt/{__version__}"

# USD per million tokens (input, output). OpenRouter prices from
# https://openrouter.ai/api/v1/models on 2026-10-13 (rechecked in step 3); Anthropic list prices.
# The Settings page can refresh the OpenRouter list and add custom prices.
KNOWN_PRICES = {
    "deepseek/deepseek-v4.1-flash": (0.0441, 0.63),
    "deepseek/deepseek-v4-flash": (0.036, 1.28),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "openai/gpt-5-nano": (0.05, 0.40),
    "openai/gpt-5-mini": (0.25, 2.00),
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "anthropic/claude-sonnet-5.5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
FALLBACK_PRICE = (1.00, 5.00)  # used for unknown models, deliberately on the high side


class LLMError(Exception):
    """A model call failed in a way the user should see (bad key, no credit...)."""


class RateLimited(LLMError):
    """The provider kept saying 'slow down'; the run stops cleanly."""


class BudgetExceeded(Exception):
    def __init__(self, scope: str, spent: float, cap: float, unit: str = "usd"):
        self.scope, self.spent, self.cap, self.unit = scope, spent, cap, unit
        if unit == "tokens":
            msg = f"{scope} token cap reached ({int(spent):,} of {int(cap):,} tokens)"
        else:
            msg = f"{scope} spending cap reached (${spent:.4f} of ${cap:.2f})"
        super().__init__(msg)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    model: str = ""
    seconds: float = 0.0
    estimated: bool = False  # True when the provider reported no usage and the tokens are our estimate


# ── prices ────────────────────────────────────────────────────────────

_price_cache: dict | None = None


def _load_price_cache() -> dict:
    global _price_cache
    if _price_cache is None:
        p = paths.data_dir() / "openrouter_prices.json"
        try:
            _price_cache = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, json.JSONDecodeError):
            _price_cache = {}
    return _price_cache


def refresh_openrouter_prices(timeout: float = 20) -> int:
    """Download OpenRouter's public price list (no key needed). Returns model count."""
    global _price_cache
    r = httpx.get(f"{OPENROUTER_URL}/models", timeout=timeout)
    r.raise_for_status()
    prices = {}
    for m in r.json().get("data", []):
        try:
            prices[m["id"]] = [float(m["pricing"]["prompt"]) * 1e6, float(m["pricing"]["completion"]) * 1e6]
        except (KeyError, TypeError, ValueError):
            continue
    (paths.data_dir() / "openrouter_prices.json").write_text(json.dumps(prices), encoding="utf-8")
    _price_cache = prices
    return len(prices)


def price_for(model: str, settings: dict | None = None) -> tuple[float, float]:
    custom = (settings or {}).get("custom_prices", {}).get(model)
    if custom:
        return float(custom.get("in", 0)), float(custom.get("out", 0))
    if model in _load_price_cache():
        return tuple(_load_price_cache()[model])
    return KNOWN_PRICES.get(model, FALLBACK_PRICE)


def cost_of(model: str, tokens_in: int, tokens_out: int, settings: dict | None = None) -> float:
    pin, pout = price_for(model, settings)
    return (tokens_in * pin + tokens_out * pout) / 1e6


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)  # deliberately pessimistic (real ratio is ~4 chars/token)


# ── the spending meter ────────────────────────────────────────────────

class Meter:
    """Counts spend for one run and for each job in it, and enforces the caps.

    `check()` is called before every request with a worst-case estimate of
    that request, so a cap stops the run *before* it is overspent rather than after.
    """

    def __init__(self, run_cap: float, job_cap: float, run_token_cap: int = 0, job_token_cap: int = 0,
                 subscription: bool = False):
        self.run_cap, self.job_cap = float(run_cap), float(job_cap)
        self.run_token_cap, self.job_token_cap = int(run_token_cap or 0), int(job_token_cap or 0)
        self.subscription = subscription  # flat-rate provider: USD caps don't apply
        self.run_cost = 0.0
        self.run_tokens_in = 0
        self.run_tokens_out = 0
        self.calls = 0
        self.job_costs: dict = {}
        self.job_tokens: dict = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: dict) -> "Meter":
        c = settings["caps"]
        return cls(c["per_run_usd"], c["per_job_usd"], c.get("per_run_tokens", 0), c.get("per_job_tokens", 0),
                   subscription=settings.get("provider") in SUBSCRIPTION_PROVIDERS)

    def check(self, job_id, estimate: float, est_tokens: int = 0) -> None:
        with self._lock:
            if not self.subscription:
                if self.run_cap > 0 and self.run_cost + estimate > self.run_cap:
                    raise BudgetExceeded("Run", self.run_cost, self.run_cap)
                if job_id is not None and self.job_cap > 0:
                    spent = self.job_costs.get(job_id, 0.0)
                    if spent + estimate > self.job_cap:
                        raise BudgetExceeded("Job", spent, self.job_cap)
            run_tokens = self.run_tokens_in + self.run_tokens_out
            if self.run_token_cap > 0 and run_tokens + est_tokens > self.run_token_cap:
                raise BudgetExceeded("Run", run_tokens, self.run_token_cap, "tokens")
            if job_id is not None and self.job_token_cap > 0:
                used = self.job_tokens.get(job_id, 0)
                if used + est_tokens > self.job_token_cap:
                    raise BudgetExceeded("Job", used, self.job_token_cap, "tokens")

    def add(self, job_id, cost: float, tokens_in: int, tokens_out: int) -> None:
        with self._lock:
            self.calls += 1
            self.run_cost += cost
            self.run_tokens_in += tokens_in
            self.run_tokens_out += tokens_out
            if job_id is not None:
                self.job_costs[job_id] = self.job_costs.get(job_id, 0.0) + cost
                self.job_tokens[job_id] = self.job_tokens.get(job_id, 0) + tokens_in + tokens_out

    def seed_job(self, job_id, cost: float, tokens: int) -> None:
        """Start a job's count from what it already used in earlier runs (stored in the tracker)."""
        with self._lock:
            self.job_costs[job_id] = max(self.job_costs.get(job_id, 0.0), float(cost or 0.0))
            self.job_tokens[job_id] = max(self.job_tokens.get(job_id, 0), int(tokens or 0))

    def move_job(self, old_key, new_key) -> None:
        """A listing became a tracked job: carry its spend over."""
        with self._lock:
            if old_key in self.job_costs:
                self.job_costs[new_key] = self.job_costs.get(new_key, 0.0) + self.job_costs.pop(old_key)
            if old_key in self.job_tokens:
                self.job_tokens[new_key] = self.job_tokens.get(new_key, 0) + self.job_tokens.pop(old_key)

    def snapshot(self) -> dict:
        with self._lock:
            return {"run_cost": self.run_cost, "run_cap": self.run_cap, "job_cap": self.job_cap,
                    "run_token_cap": self.run_token_cap, "job_token_cap": self.job_token_cap,
                    "subscription": self.subscription, "calls": self.calls,
                    "tokens_in": self.run_tokens_in, "tokens_out": self.run_tokens_out,
                    "job_costs": {str(k): v for k, v in self.job_costs.items() if isinstance(k, int)},
                    "job_tokens": {str(k): v for k, v in self.job_tokens.items() if isinstance(k, int)}}


# ── JSON helpers ──────────────────────────────────────────────────────

def extract_json(text: str):
    """Pull the first JSON object out of a model reply (tolerates ``` fences and chatter)."""
    if not text:
        raise ValueError("empty reply")
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object in reply")


# ── the client ────────────────────────────────────────────────────────

class LLMClient:
    """One object per task. `recorder(stage, job_id, response)` is called after each call."""

    def __init__(self, settings: dict, api_key: str, meter: Meter | None = None, recorder=None,
                 transport: httpx.BaseTransport | None = None, anthropic_http_client=None, session_prefix: str = ""):
        self.settings = settings
        self.provider = settings.get("provider", "opencode_go")
        self.subscription = self.provider in SUBSCRIPTION_PROVIDERS
        self.session_prefix = session_prefix or f"{time.time():.0f}"
        self._key = api_key
        self.meter = meter
        self.recorder = recorder
        self._transport = transport
        self._anthropic_http_client = anthropic_http_client
        if not api_key and self.provider != "openai_compatible":
            raise LLMError("No API key saved yet. Add one on the Accounts page.")

    def __repr__(self):  # never show the key
        return f"<LLMClient provider={self.provider}>"

    def model_for(self, stage: str) -> str:
        model = self.settings.get("models", {}).get(stage, "")
        if not model:
            raise LLMError(f"No model set for the '{stage}' step. Pick one on the Settings page.")
        return model

    def session_id(self, job_id, stage: str) -> str:
        """Stable, opaque id per job + stage (OpenCode Go requires one)."""
        raw = f"{self.session_prefix}:{job_id}:{stage}"
        return "jobhunt-" + hashlib.sha1(raw.encode()).hexdigest()[:24]

    def chat(self, *, stage: str, system: str, user: str, tools: list | None = None,
             max_tokens: int = 2000, json_mode: bool = False, job_id=None) -> LLMResponse:
        model = self.model_for(stage)
        if self.provider == "anthropic" and re.match(r"claude-(opus|sonnet|haiku|fable)-5", model):
            # these models always think a little first, and thinking counts against max_tokens:
            # leave room so a short answer (the filter's) doesn't come back empty
            max_tokens = max(max_tokens, 2000)
        est_in = estimate_tokens(system + user + json.dumps(tools or []))
        if self.meter:
            est_cost = 0.0 if self.subscription else cost_of(model, est_in, max_tokens, self.settings)
            self.meter.check(job_id, est_cost, est_in + max_tokens)
        t0 = time.monotonic()
        if self.provider == "anthropic":
            resp = self._anthropic(model, system, user, tools, max_tokens)
        else:
            resp = self._openai_style(model, system, user, tools, max_tokens, json_mode, self.session_id(job_id, stage))
        resp.seconds = time.monotonic() - t0
        resp.model = model
        if not resp.tokens_in and not resp.tokens_out:
            # Some endpoints don't report usage. Count a pessimistic estimate rather
            # than zero, so the caps still work.
            resp.estimated = True
            resp.tokens_in = est_in
            resp.tokens_out = estimate_tokens(resp.text + json.dumps([c.args for c in resp.tool_calls]))
        if self.subscription:
            resp.cost = 0.0  # included in the subscription
        elif not resp.cost:
            resp.cost = cost_of(model, resp.tokens_in, resp.tokens_out, self.settings)
        if self.meter:
            self.meter.add(job_id, resp.cost, resp.tokens_in, resp.tokens_out)
        if self.recorder:
            self.recorder(stage, job_id, resp)
        return resp

    def chat_json(self, **kw) -> tuple[dict, LLMResponse]:
        resp = self.chat(json_mode=True, **kw)
        try:
            return extract_json(resp.text), resp
        except ValueError as e:
            raise LLMError(f"The model did not return valid JSON ({e}).") from None

    # OpenRouter and other OpenAI-style endpoints
    def _openai_style(self, model, system, user, tools, max_tokens, json_mode, session="") -> LLMResponse:
        if self.provider == "openrouter":
            base = OPENROUTER_URL
        elif self.provider == "opencode_go":
            base = OPENCODE_GO_URL
        else:
            base = (self.settings.get("base_url") or "").rstrip("/")
            if not base:
                raise LLMError("Set the endpoint URL on the Settings page.")
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                              "parameters": t["parameters"]}} for t in tools]
            body["tool_choice"] = "auto"
        elif json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        if self.provider == "openrouter":
            body["usage"] = {"include": True}
            body["reasoning"] = {"enabled": bool(self.settings.get("reasoning"))}
            headers["HTTP-Referer"] = "http://127.0.0.1"
            headers["X-Title"] = "JobHunt"
        if self.provider == "opencode_go":
            headers["x-opencode-session"] = session or self.session_id(None, "misc")
            # With thinking on and a tight max_tokens, Go can spend the whole budget
            # reasoning and return nothing; so it is off unless you turn it on.
            body["thinking"] = {"type": "enabled" if self.settings.get("reasoning") else "disabled"}
        data = self._post_with_retry(f"{base}/chat/completions", headers, body)
        if data.get("error"):
            raise LLMError(f"Model error: {str(data['error'].get('message', data['error']))[:300]}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_unparsed": fn.get("arguments", "")}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), args=args if isinstance(args, dict) else {}))
        usage = data.get("usage") or {}
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
        )

    def _post_with_retry(self, url, headers, body) -> dict:
        delay = 2.0
        for attempt in range(4):
            try:
                with httpx.Client(transport=self._transport, timeout=180) as client:
                    r = client.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise LLMError(f"Could not reach the model provider ({type(e).__name__}).") from None
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                try:
                    wait = min(60.0, float(r.headers.get("retry-after", delay)))
                except ValueError:
                    wait = delay
                time.sleep(wait)
                delay *= 2
                continue
            if r.status_code == 429:
                raise RateLimited("The AI provider's rate limit was reached, so the run stopped cleanly. "
                                  "Wait a while (subscription limits reset over time) and start again.")
            if r.status_code == 401:
                raise LLMError("The API key was rejected. Check it on the Accounts page.")
            if r.status_code == 402:
                raise LLMError("The provider says the account is out of credit.")
            if r.status_code >= 400:
                raise LLMError(f"Model provider error {r.status_code}: {r.text[:300]}")
            return r.json()
        raise LLMError("The model provider kept failing; try again later.")

    # Anthropic, through the official SDK
    def _anthropic(self, model, system, user, tools, max_tokens) -> LLMResponse:
        import anthropic

        # Always the real Anthropic API: the Settings "Endpoint URL" belongs to the custom
        # OpenAI-compatible provider only, and an Anthropic key must never go anywhere else.
        kwargs = {"api_key": self._key, "max_retries": 3}
        if self._anthropic_http_client is not None:
            kwargs["http_client"] = self._anthropic_http_client
        client = anthropic.Anthropic(**kwargs)
        req = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if tools:
            req["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                            for t in tools]
        if re.match(r"claude-(opus|sonnet|haiku|fable)-5", model):
            # current models think adaptively; effort is the cost knob
            req["output_config"] = {"effort": "medium" if self.settings.get("reasoning") else "low"}
        try:
            msg = client.messages.create(**req)
        except anthropic.AuthenticationError:
            raise LLMError("The API key was rejected. Check it on the Accounts page.") from None
        except anthropic.RateLimitError:
            raise RateLimited("Anthropic rate limit hit, so the run stopped cleanly; try again in a few minutes.") from None
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic error {e.status_code}: {str(e.message)[:300]}") from None
        except anthropic.APIConnectionError:
            raise LLMError("Could not reach the Anthropic API.") from None
        if msg.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        text, calls = [], []
        for block in msg.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input or {})))
        u = msg.usage
        tokens_in = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) + \
                    (getattr(u, "cache_creation_input_tokens", 0) or 0)
        return LLMResponse(text="".join(text), tool_calls=calls, tokens_in=tokens_in, tokens_out=u.output_tokens or 0)
