"""OpenAI-compatible chat client for the enrichment pipeline (#14).

API keys are read from the desktop keyring at call time and never touch the
repo, `data/`, a log line or an argv string. The client speaks the
OpenAI-compatible chat API, so any provider or self-hosted gateway that
exposes one plugs in by editing the PROVIDERS table below — nothing else
in the codebase knows which vendor is behind it.
"""

import logging
import os
import random
import subprocess
import time

import requests

log = logging.getLogger("pubbrain")

# 429 is the rate window; 5xx on these providers is usually a transient upstream
# fault, which a full run of 1,328 requests will meet several times.
RETRY_STATUS = {402, 408, 409, 425, 429, 500, 502, 503, 504, 529}

# Some providers signal a momentarily spent quota window with either of these.
# 402 reads like a billing wall and is not always one: during the #5 backfill
# it arrived ~4 times an hour interleaved with thousands of successes, and
# waiting cleared it.
QUOTA_STATUS = {402, 429}

# Ceiling on a single rate-limit wait. Longer than any regeneration interval
# worth sitting through, short enough to notice a genuinely spent budget.
RATE_LIMIT_MAX_DELAY = 900.0

# `offer` is what the workbench's model picker lists (#39): a curated
# shortlist, not the provider's whole catalogue — the settings page reports
# the full catalogue size, so the shortlist is visibly a shortlist. Adding a
# model is a line here.
#
# In production the requests go through a self-hosted LLM gateway: the real
# provider key lives on the gateway host and this client holds only a
# revocable virtual key. The gateway routes on the model-name prefix, which
# is why every id carries one. Point GATEWAY at any OpenAI-compatible
# endpoint — a vendor API works exactly the same way.
GATEWAY = os.environ.get("PUBBRAIN_LLM_BASE_URL",
                         "https://llm-gateway.example.internal/v1")

PROVIDERS = {
    "default": {
        "base_url": GATEWAY,
        "secret": ["application", "pub-brain", "key", "api"],
        # Env name matches the calendar deployment, so one `.env` can serve
        # both services on the same box (#48).
        "env": "PUBBRAIN_LLM_API_KEY",
        "default_model": "provider/large-instruct",
        "offer": [
            "provider/large-instruct",
            "provider/small-instruct",
            "provider/large-vision",
            "provider/small-vision",
        ],
    },
}

DEFAULT_PROVIDER = "default"


class NoApiKey(RuntimeError):
    pass


def _provider(name):
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}; have {list(PROVIDERS)}") from None


def api_key(provider=DEFAULT_PROVIDER) -> str:
    """The provider's key: environment first, then the desktop keyring (#48).

    The keyring is right on the workstation — the key never touches the repo,
    `data/`, a log line or an argv string. It is unavailable on a headless
    box, which is what blocks both the server move and the weekly job (#8),
    so an env var wins when one is set: the value comes from a `chmod 600`
    `.env` the service reads, not from a shell export, so it is no more
    exposed than the keyring entry.
    """
    env = _provider(provider)["env"]
    from_env = os.environ.get(env, "").strip()
    if from_env:
        return from_env
    attrs = _provider(provider)["secret"]
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", *attrs],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError as exc:
        raise NoApiKey("secret-tool is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise NoApiKey("keyring did not respond — is it unlocked?") from exc
    key = proc.stdout.strip()
    if not key:
        raise NoApiKey(f"no keyring entry for {' '.join(attrs)} and ${env} is "
                       f"unset (rc={proc.returncode})")
    return key


def has_api_key(provider=DEFAULT_PROVIDER) -> bool:
    """Whether a key can be fetched, without returning or logging it — the
    Settings page reports reachability, never the secret itself."""
    try:
        return bool(api_key(provider))
    except NoApiKey:
        return False


def _headers(provider):
    return {
        "Authorization": f"Bearer {api_key(provider)}",
        "Content-Type": "application/json",
    }


def models(provider=DEFAULT_PROVIDER, timeout=60) -> list:
    r = requests.get(
        f"{_provider(provider)['base_url']}/models",
        headers=_headers(provider), timeout=timeout,
    )
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def offered(provider=DEFAULT_PROVIDER) -> list:
    """The shortlist the picker lists, default first. Local — no HTTP, because
    this is drawn on every record page."""
    cfg = _provider(provider)
    default = cfg["default_model"]
    rest = [m for m in cfg.get("offer", []) if m != default]
    return [default, *rest]


# A provider's full catalogue changes when the subscription does, not when a
# page is loaded, so it is cached for the process. Only the settings page reads
# it, and only to say how much the shortlist is leaving out.
_MODEL_CACHE = {}


def catalogue_size(provider=DEFAULT_PROVIDER, timeout=10):
    """How many models the provider actually carries, or None if it cannot be
    reached. Never raises — this is a footnote, not a dependency."""
    if provider not in _MODEL_CACHE:
        try:
            _MODEL_CACHE[provider] = len(models(provider, timeout=timeout))
        except Exception as exc:                   # noqa: BLE001 — see docstring
            log.debug("could not list %s models (%s)", provider, exc)
            _MODEL_CACHE[provider] = None
    return _MODEL_CACHE[provider]


def chat(messages, model=None, provider=DEFAULT_PROVIDER, temperature=None,
         max_tokens=4000, timeout=600, reasoning_effort=None) -> dict:
    """One completion. Returns content, token usage and wall-clock latency —
    latency matters as much as tokens: a full run is latency x 1,328."""
    cfg = _provider(provider)
    payload = {
        "model": model or cfg["default_model"],
        "messages": messages,
        "max_tokens": max_tokens,
    }
    # Sonnet 5 rejects a non-default temperature outright, so it is only sent
    # when a caller explicitly asks for one.
    if temperature is not None:
        payload["temperature"] = temperature
    # Some models reason before answering; "none" turns that off where supported.
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    started = time.monotonic()
    r = requests.post(
        f"{cfg['base_url']}/chat/completions",
        headers=_headers(provider), json=payload, timeout=timeout,
    )
    elapsed = time.monotonic() - started
    r.raise_for_status()
    body = r.json()
    usage = body.get("usage") or {}
    message = body["choices"][0]["message"]
    # Reasoning models return content=null and put the text in reasoning_content.
    content = message.get("content") or message.get("reasoning_content") or ""
    return {
        "content": content,
        "finish_reason": body["choices"][0].get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "seconds": round(elapsed, 2),
        "model": body.get("model", payload["model"]),
    }


def _retry_delay(exc, attempt, base, rate_limited=False):
    """Honour Retry-After when the provider sends one, else exponential backoff
    with jitter so a resumed run does not re-synchronise onto the same window.

    A 429 is a spent quota window rather than a blip, so it waits in minutes:
    an unattended backfill should ride the window out, not abandon the run.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            return max(1.0, float(response.headers.get("Retry-After")))
        except (TypeError, ValueError):
            pass
    if rate_limited:
        return min(RATE_LIMIT_MAX_DELAY, 60.0 * (2 ** attempt)) * (1 + random.random() * 0.25)
    return base * (2 ** attempt) * (1 + random.random() * 0.25)


def chat_with_backoff(messages, retries=8, base_delay=5.0, sleep=time.sleep, **kwargs):
    """chat(), retrying rate limits and transient upstream faults. Any other
    error — a bad model name, a rejected key — raises on the first attempt.

    The defaults buy roughly an hour of patience against a rate limit, which is
    long enough to cross a quota window and short enough that a genuinely
    exhausted budget still surfaces the same night.
    """
    for attempt in range(retries + 1):
        try:
            return chat(messages, **kwargs)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in RETRY_STATUS or attempt == retries:
                raise
            delay = _retry_delay(exc, attempt, base_delay,
                                 rate_limited=status in QUOTA_STATUS)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == retries:
                raise
            delay = _retry_delay(exc, attempt, base_delay)
            status = type(exc).__name__
        log.warning("%s — retrying in %.0fs (attempt %d/%d)",
                    status, delay, attempt + 1, retries)
        sleep(delay)
