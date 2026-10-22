import pytest

from countersign.model import Model, ModelError, SpendCapReached, config_from_env

from fakes import Script, fake_model


def test_no_provider_means_no_model():
    assert config_from_env({}) is None and config_from_env({"CS_MODEL_PROVIDER": "none"}) is None
    assert Model.from_env({}) is None


def test_vultr_is_the_default_shape_and_the_key_is_never_shown():
    cfg = config_from_env({"CS_MODEL_PROVIDER": "vultr", "VULTR_INFERENCE_API_KEY": "vk-secret"})
    assert cfg.base_url == "https://api.vultrinference.com/v1" and cfg.key_header == "Authorization"
    assert cfg.text_model == cfg.vision_model and "vk-secret" not in repr(cfg)


def test_a_configured_provider_without_a_key_is_an_error_not_a_silent_fallback():
    with pytest.raises(ModelError, match="no API key"):
        config_from_env({"CS_MODEL_PROVIDER": "vultr"})
    with pytest.raises(ModelError, match="unknown"):
        config_from_env({"CS_MODEL_PROVIDER": "mystery"})


def test_azure_sends_its_key_in_its_own_header():
    script = Script(map={"values": []})
    cfg = config_from_env({"CS_MODEL_PROVIDER": "azure", "AZURE_OPENAI_BASE_URL": "https://x.azure.com/openai/v1",
                           "AZURE_OPENAI_API_KEY": "az-secret"})
    Model(cfg, transport=script).json(purpose="map", system="You fill web forms", user="u")
    req = script.requests[0]
    assert req["url"] == "https://x.azure.com/openai/v1/chat/completions"
    assert req["headers"]["api-key"] == "az-secret" and "Authorization" not in req["headers"]
    assert req["body"]["response_format"] == {"type": "json_object"}


def test_json_is_parsed_even_inside_a_fence_and_spend_is_recorded(tmp_path):
    m = fake_model(Script(map='```json\n{"values": [1]}\n```'), tmp_path)
    assert m.json(purpose="map", system="You fill web forms", user="u") == {"values": [1]}
    assert m.session.calls == 1 and m.session.usd == pytest.approx(0.003)  # 1000 in at $2/M + 100 out at $10/M
    assert m.spent() == pytest.approx(0.003)
    assert (tmp_path / "spend.jsonl").read_text().count("\n") == 1


def test_an_answer_that_is_not_json_is_retried_once_then_raises(tmp_path):
    script = Script(map="I would rather not.")
    m = fake_model(script, tmp_path)
    with pytest.raises(ModelError, match="not a JSON object"):
        m.json(purpose="map", system="You fill web forms", user="u")
    assert len(script.requests) == 2


def test_the_cap_stops_a_call_before_it_is_sent(tmp_path):
    script = Script(map={"values": []})
    m = fake_model(script, tmp_path, cap=0.003)
    m.json(purpose="map", system="You fill web forms", user="u", max_tokens=10)
    with pytest.raises(SpendCapReached, match="spend cap"):
        m.json(purpose="map", system="You fill web forms", user="u", max_tokens=10)
    assert len(script.requests) == 1


def test_images_go_as_data_urls_to_the_vision_model(tmp_path):
    script = Script(witness={"fields": []})
    m = fake_model(script, tmp_path)
    m.json(purpose="witness", system="check", user="u", images=[b"\x89PNG fake"])
    body = script.requests[0]["body"]
    assert body["model"] == "fake-vision-1"
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_reply_without_usage_is_metered_at_the_estimate_not_zero(tmp_path):
    class NoUsage(Script):
        def __call__(self, *a):
            r = super().__call__(*a)
            r.pop("usage")
            return r
    m = fake_model(NoUsage(map={"values": []}), tmp_path)
    m.json(purpose="map", system="You fill web forms", user="u" * 400, max_tokens=100)
    assert m.session.usd > 0 and '"estimated": true' in (tmp_path / "spend.jsonl").read_text()


def test_the_ledger_is_read_incrementally_and_shared_between_processes(tmp_path):
    a = fake_model(Script(map={"values": []}), tmp_path)
    b = fake_model(Script(map={"values": []}), tmp_path)
    a.json(purpose="map", system="You fill web forms", user="u")
    assert b.spent() == pytest.approx(0.003)
    b.json(purpose="map", system="You fill web forms", user="u")
    assert a.spent() == pytest.approx(0.006) and b.spent() == pytest.approx(0.006)
