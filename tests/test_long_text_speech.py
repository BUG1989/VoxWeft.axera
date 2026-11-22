import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from voxweft.runtime import server
from voxweft.runtime.orchestrator import SpeechSynthesisError


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

    async def _synth_and_send(self, seq, text, voice_role, **kwargs):
        self.synthesized.append((seq, text, voice_role))
        return {"started": True, "samples": 160, "interrupted": False}

    async def send_json(self, event):
        pass


async def send_json(event):
    pass


class RetryableFakeOrchestrator(FakeOrchestrator):
    def __init__(self):
        super().__init__()
        self.failures_remaining = 1

    async def _synth_and_send(self, seq, text, voice_role, **kwargs):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise SpeechSynthesisError("connection reset", sent_samples=0)
        return await super()._synth_and_send(seq, text, voice_role)


class PartialFailureFakeOrchestrator(FakeOrchestrator):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def _synth_and_send(self, seq, text, voice_role, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise SpeechSynthesisError("stream stopped", sent_samples=480)
        return await super()._synth_and_send(seq, text, voice_role)


class SourcePartialFailureFakeOrchestrator(FakeOrchestrator):
    def __init__(self):
        super().__init__()
        self.failed = False

    async def _synth_and_send(self, seq, text, voice_role, **kwargs):
        if not self.failed and voice_role == "source":
            self.failed = True
            raise SpeechSynthesisError("source stream stopped", sent_samples=480)
        return await super()._synth_and_send(seq, text, voice_role, **kwargs)


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


def test_speech_chunk_retries_duplicate_free_failure():
    orch = RetryableFakeOrchestrator()
    events = []

    async def send_event(event):
        events.append(event)

    chunks = ["第一句。", "第二句。"]
    completed = asyncio.run(server._speak_chunks(orch, chunks, "interpreter", send_event))

    assert completed == 2
    assert [text for _, text, _ in orch.synthesized] == chunks
    assert [event["type"] for event in events].count("speech_chunk_retry") == 1


def test_speech_chunk_partial_failure_is_resumable_without_replay():
    orch = PartialFailureFakeOrchestrator()
    events = []

    async def send_event(event):
        events.append(event)

    chunks = ["第一句。", "第二句。"]
    try:
        asyncio.run(server._speak_chunks(orch, chunks, "interpreter", send_event))
    except server.SpeechChunkFailure as failure:
        assert failure.index == 0
        assert failure.next_index == 1
        assert failure.sent_samples == 480
    else:
        raise AssertionError("partial TTS failure was not reported")

    completed = asyncio.run(server._speak_chunks(
        orch, chunks, "interpreter", send_event, start=1,
    ))
    assert completed == 2
    assert [text for _, text, _ in orch.synthesized] == ["第二句。"]


def test_target_chunk_resume_continues_from_next_chunk():
    orch = PartialFailureFakeOrchestrator()
    events = []
    failures = []

    async def send_event(event):
        events.append(event)

    async def first_turn():
        await server._text_turn(
            orch, "第一句。第二句。", send_event, speak_source=False,
            on_speech_failure=failures.append)

    asyncio.run(first_turn())
    assert len(failures) == 1
    assert failures[0].phase == "target"
    assert failures[0].resumable is True
    assert events[-1]["stage"] == "speech_chunk"
    assert events[-1]["resumable"] is True

    pending = {
        "chunks": failures[0].chunks,
        "next_index": failures[0].next_index,
        "total": failures[0].total,
        "voice_role": failures[0].voice_role,
        "phase": failures[0].phase,
        "text": failures[0].source_text,
        "translated": failures[0].translated_text,
    }
    completed = asyncio.run(server._resume_speech_turn(orch, pending, send_event))

    assert completed == 2
    assert [text for _, text, _ in orch.synthesized] == ["第二句。"]
    assert [event["type"] for event in events].count("translation") == 1
    assert events[-1]["type"] == "speech_chunk_end"
    assert events[-1]["index"] == 1


def test_source_chunk_resume_finishes_translation_and_target_speech():
    orch = SourcePartialFailureFakeOrchestrator()
    events = []
    failures = []

    async def send_event(event):
        events.append(event)

    async def first_turn():
        await server._text_turn(
            orch, "源第一句。源第二句。", send_event, speak_source=True,
            on_speech_failure=failures.append)

    asyncio.run(first_turn())
    assert len(failures) == 1
    assert failures[0].phase == "source"
    assert failures[0].resumable is True
    assert not any(event["type"] == "translation" for event in events)

    failure = failures[0]
    pending = {
        "chunks": failure.chunks,
        "next_index": failure.next_index,
        "total": failure.total,
        "voice_role": failure.voice_role,
        "phase": failure.phase,
        "text": failure.source_text,
        "translated": failure.translated_text,
    }
    completed = asyncio.run(server._resume_speech_turn(orch, pending, send_event))

    assert completed == 2
    assert [text for _, text, _ in orch.synthesized] == [
        "源第二句。", "源第一句。", "源第二句。",
    ]
    assert events[-1]["type"] == "speech_chunk_end"
    assert events[-1]["index"] == 1


@pytest.mark.parametrize("target_chars", [5000, 10000])
def test_stress_length_text_is_split_losslessly(target_chars):
    source = LONG_TEXT.strip()
    text = (source + "\n") * ((target_chars + len(source) - 1) // len(source))

    chunks = server._split_speech_text(text)
    normalized = " ".join(text.split())

    assert len(normalized) >= target_chars
    assert chunks
    assert all(0 < len(chunk) <= 180 for chunk in chunks)
    # Whitespace is normalized for TTS; verify that no visible character is lost.
    assert "".join(chunks).replace(" ", "") == normalized.replace(" ", "")
