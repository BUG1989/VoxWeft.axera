import asyncio
from dataclasses import dataclass
from pathlib import Path

from voxweft.runtime import server


LONG_TEXT = (Path(__file__).parents[1] / "tests/fixtures/long_chinese_article.txt").read_text(
    encoding="utf-8")


@dataclass
class FakeRoute:
    voice_role: str = "interpreter"
    src: str = "zh"
    tgt: str = "en"


class FakeTranslator:
    def translate(self, text, source, target):
        return {"text": text, "latency_ms": 1}


class FakeOrchestrator:
    mode = "interpreter"
    routes = FakeRoute()

    def __init__(self):
        self.engines = {"translate": FakeTranslator()}
        self.synthesized = []

    async def _run_blocking(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _synth_and_send(self, seq, text, voice_role):
        self.synthesized.append((seq, text, voice_role))

    async def send_json(self, event):
        pass


async def send_json(event):
    pass


def test_long_article_is_split_into_sentence_sized_tts_requests():
    chunks = server._split_speech_text(LONG_TEXT)

    assert len(chunks) > 10
    assert all(len(chunk) <= 180 for chunk in chunks)
    assert "".join(chunks) == "".join(LONG_TEXT.split())
    assert any("看五十年的中国就来深圳" in chunk for chunk in chunks)
    assert any("续写“亚太奇迹”" in chunk for chunk in chunks)


def test_english_sentences_split_without_breaking_decimals():
    chunks = server._split_speech_text(
        "The model runs on device. "
        "Version 3.5 supports streaming. "
        "Latency is 0.32 seconds!"
    )

    assert chunks == [
        "The model runs on device.",
        "Version 3.5 supports streaming.",
        "Latency is 0.32 seconds!",
    ]


def test_text_turn_synthesizes_every_long_text_chunk():
    orch = FakeOrchestrator()
    events = []

    async def send_event(event):
        events.append(event)

    asyncio.run(server._text_turn(orch, LONG_TEXT, send_event, speak_source=False))

    assert orch.synthesized
    assert all(len(text) <= 180 for _, text, _ in orch.synthesized)
    assert "".join(text for _, text, _ in orch.synthesized) == "".join(LONG_TEXT.split())
    assert events[-1]["type"] == "turn_end"
    assert events[-1]["sentences"] == len(orch.synthesized)
