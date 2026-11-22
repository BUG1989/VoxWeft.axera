import io

import numpy as np
import pytest

from voxweft.runtime import ax8860


def test_hymt2_translation_prompt(monkeypatch):
    monkeypatch.setattr(ax8860, "AX_MT_MODEL", "hy-mt2")
    result = ax8860.translation_prompt("今天天气很好。", "zh", "en")
    assert "将以下文本翻译为英语" in result
    assert "今天天气很好。" in result


def test_hymt2_translate(monkeypatch):
    monkeypatch.setattr(ax8860, "AX_MT_MODEL", "hy-mt2")
    calls = {}

    def fake_post(base, payload, timeout=None):
        calls["base"] = base
        calls["payload"] = payload
        return "译文：The weather is nice today."

    monkeypatch.setattr(ax8860, "_post_json", fake_post)
    result = ax8860.AXHyMT2Translator().translate("今天天气很好。", "zh", "en")
    assert result["text"] == "The weather is nice today."
    assert calls["payload"]["messages"][0]["role"] == "user"
    assert calls["payload"]["temperature"] == 0


def test_hymt2_empty_completion_retries_with_unique_prompt(monkeypatch):
    monkeypatch.setattr(ax8860, "AX_MT_MODEL", "hy-mt2")
    outputs = iter(["", "The weather is nice today."])
    prompts = []

    def fake_post(base, payload, timeout=None):
        prompts.append(payload["messages"])
        return next(outputs)

    monkeypatch.setattr(ax8860, "_post_json", fake_post)
    result = ax8860.AXHyMT2Translator().translate("今天天气很好。", "zh", "en")
    assert result["text"] == "The weather is nice today."
    assert len(prompts) == 2
    assert prompts[0][0]["role"] == "user"
    assert prompts[1][0]["role"] == "system"
    assert "Request ID:" in prompts[1][0]["content"]


def test_hymt2_rejects_unsupported_language():
    with pytest.raises(RuntimeError, match="does not advertise"):
        ax8860.AXHyMT2Translator().translate("hello", "en", "lo")


def test_transcribe_parses_ax_output(monkeypatch):
    def fake_post(base, payload, timeout=None):
        return "language Chinese<asr_text>你好，世界"

    monkeypatch.setattr(ax8860, "_post_json", fake_post)
    result = ax8860.transcribe(np.zeros(1600, dtype=np.float32), "Chinese")
    assert result["text"] == "你好，世界"


def test_health_includes_model_catalog(monkeypatch):
    class Response:
        ok = True
        status_code = 200
        def json(self):
            return {"data": [{"id": "fixture-model"}]}

    monkeypatch.setattr(ax8860.requests, "get", lambda url, timeout: Response())
    result = ax8860.health()
    assert [item["stage"] for item in result["models"]] == ["ASR", "翻译", "TTS"]
    assert result["models"][0]["name"] == "Qwen3-ASR-1.7B"
    assert result["models"][0]["parameters"] == "1.7B"
    assert result["models"][0]["ok"] is True
    assert result["models"][0]["runtime_models"] == ["fixture-model"]


def test_post_json_retries_transient_5xx(monkeypatch):
    class Response:
        status_code = 503
        text = "service unavailable"
        def close(self): pass
    responses = iter([
        Response(),
        type("Response", (), {
            "status_code": 200,
            "text": "",
            "json": lambda self: {"choices": [{"message": {"content": "ok"}}]},
            "close": lambda self: None,
        })(),
    ])
    sleeps = []
    monkeypatch.setattr(ax8860.requests, "post", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(ax8860.time, "sleep", lambda value: sleeps.append(value))

    assert ax8860._post_json("http://board/v1", {}) == "ok"
    assert sleeps == [ax8860.AX_HTTP_RETRY_DELAY]


def test_post_json_closes_http_error_response(monkeypatch):
    class Response:
        status_code = 400
        text = "bad request"
        closed = False
        def close(self):
            self.closed = True

    response = Response()
    monkeypatch.setattr(ax8860.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError, match="HTTP 400"):
        ax8860._post_json("http://board/v1", {})
    assert response.closed is True


def test_tts_stream_retries_5xx_and_closes_failed_response(monkeypatch):
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}
            self.closed = False
        def raise_for_status(self):
            pass
        def close(self):
            self.closed = True

    failed = Response(503)
    responses = [failed, Response(200)]
    sleeps = []
    monkeypatch.setattr(
        ax8860.requests, "post", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(ax8860.time, "sleep", lambda value: sleeps.append(value))

    response = ax8860._open_tts_stream({"input": "hello"})
    assert response.status_code == 200
    assert failed.closed is True
    assert sleeps == [ax8860.AX_HTTP_RETRY_DELAY]


def test_tts_stream_retries_connection_before_audio(monkeypatch):
    class Response:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def close(self): pass
    calls = {"count": 0}
    def fake_post(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ax8860.requests.ConnectionError("reset")
        return Response()
    monkeypatch.setattr(ax8860.requests, "post", fake_post)
    monkeypatch.setattr(ax8860.time, "sleep", lambda value: None)

    response = ax8860._open_tts_stream({"input": "hello"})
    assert response.status_code == 200


def test_language_pair_validation():
    assert ax8860.validate_language_pair("zh", "en") == ("zh", "en")
    assert ax8860.supported_languages() == ["de", "en", "es", "fr", "ja", "ko", "ru", "zh"]
    with pytest.raises(ValueError, match="must be different"):
        ax8860.validate_language_pair("en", "eng_Latn")
    with pytest.raises(RuntimeError, match="does not advertise"):
        ax8860.validate_language_pair("zh", "lo")


def test_reference_voice_builds_cached_clone_payload():
    voice = ax8860._reference_voice("zh_tgt")
    assert set(voice) == {"ref_audio", "ref_text"}
    assert len(voice["ref_audio"]) > 1000
    assert "VoxWeft" in voice["ref_text"]
    assert ax8860._reference_voice("zh_tgt") is not voice
    assert ax8860._reference_voice("missing_tgt") == {}
