"""A scripted model endpoint, so the model-driven code is tested without a network or a bill."""

from __future__ import annotations

import json

from countersign.model import Model, ModelConfig, ModelError


class Script:
    """Answers each chat-completions request by the first word of its purpose: the
    system prompt tells learn, map, witness and confirm apart."""

    def __init__(self, **answers):
        self.answers = answers  # purpose -> dict, str (raw text) or Exception
        self.requests: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": body})
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        text = user if isinstance(user, str) else user[0]["text"]
        purpose = ("learn" if "You read web forms" in system else "map" if "You fill web forms" in system
                   else "confirm" if "Did the site accept" in text else "witness")
        answer = self.answers.get(purpose, {})
        if isinstance(answer, Exception):
            raise answer
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return {"model": "fake-1", "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


def fake_model(script: Script, tmp_path=None, cap: float = 5.0) -> Model:
    cfg = ModelConfig("fake", "https://model.invalid/v1", "sk-test", "fake-1", "fake-vision-1", 2.0, 10.0)
    return Model(cfg, ledger=(tmp_path / "spend.jsonl") if tmp_path else None, cap_usd=cap, transport=script)


__all__ = ["Script", "fake_model", "ModelError"]
