"""The control plane's one door to a language model.

OpenAI-compatible chat completions over HTTPS. In production that is Vultr Serverless
Inference; for development, any OpenAI-compatible endpoint (Azure OpenAI, a local vLLM).
The key lives here, on the control plane, and never goes into a sandbox.

Every call is metered: tokens and dollars go into a ledger file, and a call that would
cross the spend cap is refused before it is sent. A call that fails, or returns
something that is not the JSON object asked for, raises; nothing here returns a
quiet default.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

VULTR_BASE = "https://api.vultrinference.com/v1"
# Read from https://api.vultrinference.com/v1/models on 2026-10-21: both accept images.
VULTR_DEFAULT_MODEL = "qwen3.8-27b"
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ModelError(Exception):
    """The model could not be reached, or its answer could not be used."""


class SpendCapReached(ModelError):
    """The call would cross the spend cap. It was not sent."""


class ModelRefused(ModelError):
    """The provider's own content filter refused the prompt. `categories` says why
    (Azure names "jailbreak" when it sees a prompt attack in what it was given)."""

    def __init__(self, message: str, categories: list[str]):
        super().__init__(message)
        self.categories = categories


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    text_model: str
    vision_model: str
    price_in: float  # USD per 1M input tokens
    price_out: float  # USD per 1M output tokens
    key_header: str = "Authorization"  # "Authorization" (Bearer) or "api-key" (Azure)
    extra: dict = field(default_factory=dict)  # sent with every request, e.g. reasoning_effort

    @property
    def label(self) -> str:
        same = self.text_model == self.vision_model
        return f"{self.provider}:{self.text_model}" + ("" if same else f" (vision {self.vision_model})")


def config_from_env(env=None) -> ModelConfig | None:
    """None when no model is configured. Raises when one is configured badly."""
    env = os.environ if env is None else env
    provider = env.get("CS_MODEL_PROVIDER", "").strip().lower()
    if not provider or provider == "none":
        return None

    def price(name, default):
        try:
            return float(env.get(name, default))
        except ValueError as exc:
            raise ModelError(f"{name} is not a number") from exc

    extra = {}
    if env.get("CS_REASONING_EFFORT"):
        extra["reasoning_effort"] = env["CS_REASONING_EFFORT"]
    if provider == "vultr":
        base, key = env.get("CS_MODEL_BASE_URL", VULTR_BASE), env.get("VULTR_INFERENCE_API_KEY") or env.get("CS_MODEL_API_KEY", "")
        text = env.get("CS_MODEL", VULTR_DEFAULT_MODEL)
        header, p_in, p_out = "Authorization", price("CS_PRICE_IN", "1.0"), price("CS_PRICE_OUT", "1.0")
    elif provider == "azure":
        base, key = env.get("AZURE_OPENAI_BASE_URL", ""), env.get("AZURE_OPENAI_API_KEY", "")
        text = env.get("CS_MODEL", "gpt-6.1-sol")
        header, p_in, p_out = "api-key", price("CS_PRICE_IN", "2.0"), price("CS_PRICE_OUT", "10.0")
        extra.setdefault("reasoning_effort", "low")
    elif provider in ("openai", "local"):
        base, key = env.get("CS_MODEL_BASE_URL", ""), env.get("CS_MODEL_API_KEY", "")
        text = env.get("CS_MODEL", "")
        header, p_in, p_out = "Authorization", price("CS_PRICE_IN", "0"), price("CS_PRICE_OUT", "0")
    else:
        raise ModelError(f"unknown CS_MODEL_PROVIDER {provider!r} (vultr, azure, openai, local, none)")
    if not base or not text:
        raise ModelError(f"CS_MODEL_PROVIDER={provider} needs a base URL and a model name")
    if not key and provider != "local":
        raise ModelError(f"CS_MODEL_PROVIDER={provider} has no API key in the environment")
    return ModelConfig(provider, base.rstrip("/"), key, text, env.get("CS_VISION_MODEL", text), p_in, p_out, header, extra)


Transport = Callable[[str, dict, dict, float], dict]


def _http(url: str, headers: dict, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:8000].decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error") or {}
        except (ValueError, AttributeError):
            err = {}
        if isinstance(err, dict) and err.get("code") == "content_filter":
            found = ((err.get("innererror") or {}).get("content_filter_result") or {})
            cats = sorted(k for k, v in found.items() if isinstance(v, dict) and (v.get("filtered") or v.get("detected")))
            raise ModelRefused(f"the provider's content filter refused the prompt ({', '.join(cats) or 'no category given'})",
                               cats) from exc
        raise ModelError(f"model endpoint answered {exc.code}: {raw[:400]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelError(f"model endpoint unreachable: {exc}") from exc
    except ValueError as exc:
        raise ModelError("model endpoint sent something that is not JSON") from exc


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


class Model:
    def __init__(self, cfg: ModelConfig, *, ledger: Path | str | None = None, cap_usd: float = 5.0,
                 transport: Transport | None = None, timeout: float = 120.0):
        self.cfg, self.cap_usd, self.timeout = cfg, cap_usd, timeout
        self.ledger = Path(ledger) if ledger else None
        self._send = transport or _http
        self._lock = threading.Lock()
        self.session = Usage()
        self._ledger_seen = (0, 0.0)  # (bytes read, dollars in them): the ledger is only ever appended to

    @classmethod
    def from_env(cls, env=None, **kw) -> "Model | None":
        env = os.environ if env is None else env
        cfg = config_from_env(env)
        if cfg is None:
            return None
        cap = float(env.get("CS_SPEND_CAP_USD", "5"))
        return cls(cfg, ledger=env.get("CS_LEDGER", "runs/spend.jsonl"), cap_usd=cap, **kw)

    # metering

    def spent(self) -> float:
        """Dollars recorded in the ledger, across every process that used it."""
        if not self.ledger or not self.ledger.exists():
            return self.session.usd
        offset, total = self._ledger_seen
        if self.ledger.stat().st_size < offset:  # replaced or truncated: start over
            offset, total = 0, 0.0
        with self.ledger.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1  # a half-written last line is read next time
        for line in chunk[:end].splitlines():
            try:
                total += float(json.loads(line).get("usd") or 0)
            except ValueError:
                continue
        self._ledger_seen = (offset + end, total)
        return total

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.cfg.price_in + tokens_out * self.cfg.price_out) / 1e6

    def _record(self, purpose: str, model: str, usage: dict, seconds: float, guess: tuple[int, int] = (0, 0)) -> None:
        estimated = not usage.get("prompt_tokens") and not usage.get("completion_tokens")
        # A provider that reports no usage is metered at the pre-call estimate, never at zero.
        t_in, t_out = guess if estimated else (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        usd = self._cost(t_in, t_out)
        self.session.calls += 1
        self.session.tokens_in += t_in
        self.session.tokens_out += t_out
        self.session.usd += usd
        if self.ledger:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            row = {"t": round(time.time(), 3), "purpose": purpose, "provider": self.cfg.provider, "model": model,
                   "in": t_in, "out": t_out, "usd": round(usd, 6), "s": round(seconds, 2), "estimated": estimated}
            with self.ledger.open("a") as fh:
                fh.write(json.dumps(row) + "\n")

    # calls

    def json(self, *, purpose: str, system: str, user: str, images: list[bytes] = (), max_tokens: int = 4000) -> dict:
        """Ask for one JSON object. Raises ModelError if none comes back."""
        model = self.cfg.vision_model if images else self.cfg.text_model
        content: list | str = user
        if images:
            content = [{"type": "text", "text": user}]
            for png in images:
                if len(png) > MAX_IMAGE_BYTES:
                    raise ModelError(f"screenshot of {len(png)} bytes is over the {MAX_IMAGE_BYTES} byte limit")
                url = "data:image/png;base64," + base64.b64encode(png).decode()
                content.append({"type": "image_url", "image_url": {"url": url}})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        # A rough upper bound before sending: 4 characters a token, ~1.5k tokens an image.
        guess_tokens = (len(system + user) // 4 + 1500 * len(images), max_tokens)
        guess = self._cost(*guess_tokens)
        with self._lock:
            if self.spent() + guess > self.cap_usd:
                raise SpendCapReached(
                    f"spend cap ${self.cap_usd:.2f} would be crossed (spent ${self.spent():.4f}, this call up to ${guess:.4f})")
        last_error = "no answer"
        for attempt in range(2):
            body = {"model": model, "messages": messages, "max_completion_tokens": max_tokens,
                    "response_format": {"type": "json_object"}, **self.cfg.extra}
            if self.cfg.provider in ("vultr", "openai", "local"):
                body["max_tokens"] = body.pop("max_completion_tokens")
            headers = {"Content-Type": "application/json"}
            headers[self.cfg.key_header] = (f"Bearer {self.cfg.api_key}" if self.cfg.key_header == "Authorization"
                                            else self.cfg.api_key)
            began = time.monotonic()
            reply = self._send(f"{self.cfg.base_url}/chat/completions", headers, body, self.timeout)
            with self._lock:
                self._record(purpose, reply.get("model") or model, reply.get("usage") or {}, time.monotonic() - began, guess_tokens)
            try:
                text = reply["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                raise ModelError(f"model reply has no message: {str(reply)[:300]}") from None
            parsed = _parse_object(text)
            if parsed is not None:
                return parsed
            last_error = f"not a JSON object: {text[:200]!r}"
            messages = messages + [{"role": "assistant", "content": text},
                                   {"role": "user", "content": "That was not one JSON object. Reply with the JSON object only."}]
        raise ModelError(f"{purpose}: {last_error}")


def _parse_object(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
