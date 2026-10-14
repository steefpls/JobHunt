"""App settings, kept in data/settings.json.

Never put a secret in here: API keys and site passwords live in Windows
Credential Manager (see secrets_store.py). This file only holds names and knobs.
"""
import copy
import json
import shutil
import threading
import time

from . import paths

DEFAULT_MODEL = "deepseek-v4.1-flash"  # OpenCode Go's id for DeepSeek V4.1 Flash
OPENROUTER_DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

PROVIDERS = {
    "opencode_go": "OpenCode Go (flat monthly subscription)",
    "openrouter": "OpenRouter (pay per use; one key, many models)",
    "anthropic": "Anthropic API (Claude models)",
    "openai_compatible": "Other OpenAI-compatible endpoint (advanced)",
}

PROVIDER_SHORT = {"opencode_go": "OpenCode Go", "openrouter": "OpenRouter", "anthropic": "Anthropic",
                  "openai_compatible": "Custom endpoint"}

# Providers billed as a flat subscription: no per-call price, so USD caps don't
# apply there; the token caps do.
SUBSCRIPTION_PROVIDERS = {"opencode_go"}

# Sensible per-provider model defaults, used when the provider is switched.
PROVIDER_DEFAULT_MODELS = {
    "opencode_go": {"filter": DEFAULT_MODEL, "tailor": DEFAULT_MODEL, "apply": DEFAULT_MODEL, "profile": DEFAULT_MODEL},
    "openrouter": {s: OPENROUTER_DEFAULT_MODEL for s in ("filter", "tailor", "apply", "profile")},
    "anthropic": {"filter": "claude-haiku-5-5", "tailor": "claude-sonnet-5-5", "apply": "claude-sonnet-5-5", "profile": "claude-sonnet-5-5"},
    "openai_compatible": {"filter": "", "tailor": "", "apply": "", "profile": ""},
}

SITES = {
    "mycareersfuture": "MyCareersFuture (SG)",
    "jobstreet": "JobStreet (SG)",
    "linkedin": "LinkedIn Jobs",
    "indeed": "Indeed SG (experimental)",
    "demo": "Practice board (fake jobs, safe to try)",
}

DEFAULTS = {
    "provider": "opencode_go",
    "base_url": "",  # only for openai_compatible
    "models": dict(PROVIDER_DEFAULT_MODELS["opencode_go"]),
    "reasoning": False,  # let models "think" first: better answers, more output tokens
    # Spending caps. USD caps work for pay-per-use providers; token caps work for all
    # (0 = no cap). A run stops cleanly before a call that could go over a cap.
    "caps": {"per_job_usd": 0.10, "per_run_usd": 2.00, "per_job_tokens": 150000, "per_run_tokens": 3000000},
    "pacing": {
        "min_delay_s": 4.0,  # random wait between page loads on job sites
        "max_delay_s": 9.0,
        "max_apps_per_hour_per_site": 15,
        "action_delay_s": 0.8,  # pause between clicks/typing while applying
    },
    "apply": {"max_steps": 30, "show_browser": True, "allow_register": False},
    # sites_chosen: you picked sites that include a real one; until then the Hunt page suggests them (hunt.suggested_sites)
    "hunt": {"jobs": 10, "sites": ["demo"], "sites_chosen": False, "auto_submit": False, "max_candidates": 60},
    "scope": {
        "keywords": [],  # search terms typed into each job site
        "location": "Singapore",
        "avoid_words": ["intern", "internship"],  # skipped without spending tokens
    },
    # Saved logins for application sites. Only the name and the allowed sites are
    # kept here; the username and password are in Windows Credential Manager.
    "logins": [],  # [{"name": "default", "domains": ["greenhouse.io"]}]
    "custom_prices": {},  # {"model-id": {"in": usd_per_million, "out": usd_per_million}}
    "demo_board_url": "",  # set automatically to this server's /demo; tests override it
}

_lock = threading.RLock()  # update() holds it while save() takes it again


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _retry(fn, tries: int = 10):
    """On Windows a file being swapped in (or read) by another thread is briefly locked: try again."""
    for attempt in range(tries):
        try:
            return fn()
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05)


def _read_saved() -> dict:
    """What settings.json holds ({} if there is none). A file that isn't valid JSON is kept as
    settings.broken.json, so the next save can't quietly throw away what it held."""
    p = paths.settings_path()
    if not p.exists():
        return {}
    text = _retry(lambda: p.read_text(encoding="utf-8"))
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        try:
            shutil.copyfile(p, p.with_name("settings.broken.json"))
        except OSError:
            pass
        return {}
    return raw if isinstance(raw, dict) else {}


def load() -> dict:
    try:
        raw = _read_saved()
    except OSError:
        raw = {}
    return _merge(DEFAULTS, raw)


def save(settings: dict) -> None:
    with _lock:
        p = paths.settings_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        _retry(lambda: tmp.replace(p))


def update(patch: dict) -> dict:
    # Read, change and write under one lock, and never from defaults just because the file
    # couldn't be read this instant: that would wipe the provider, caps and logins.
    with _lock:
        s = _merge(_merge(DEFAULTS, _read_saved()), patch)
        save(s)
    return s
