"""Remote AX8860 engines for the Linux VoxWeft runtime.

This module deliberately contains no MLX imports.  It adapts the stable engine
contracts used by ``voxweft.runtime.orchestrator`` to llama-server endpoints on
an AX8860 board:

* Qwen3-ASR: ``POST /v1/chat/completions`` with OpenAI-style ``input_audio``
* Hy-MT2:   ``POST /v1/chat/completions`` using the model-card translation prompt
* VoxCPM2:  ``POST /v1/audio/speech`` with streamed 48 kHz int16 PCM
"""
from __future__ import annotations

import base64
import logging
import json
import io
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator

import numpy as np
import requests
from scipy.signal import resample_poly
import soundfile as sf

from voxweft.runtime.router import normalize


DEFAULT_BOARD = os.environ.get("AX_BOARD_HOST", "127.0.0.1")
AX_ASR_BASE = os.environ.get("AX_ASR_BASE", f"http://{DEFAULT_BOARD}:18081/v1").rstrip("/")
AX_MT_BASE = os.environ.get("AX_MT_BASE", f"http://{DEFAULT_BOARD}:18082/v1").rstrip("/")
AX_TTS_BASE = os.environ.get("AX_TTS_BASE", f"http://{DEFAULT_BOARD}:18083/v1").rstrip("/")
AX_STATS_URL = os.environ.get("AX_STATS_URL", f"http://{DEFAULT_BOARD}:8080/api/stats").rstrip("/")
AX_ASR_MODEL = os.environ.get("AX_ASR_MODEL", "default")
AX_MT_MODEL = os.environ.get("AX_MT_MODEL", "default")
AX_TTS_MODEL = os.environ.get("AX_TTS_MODEL", "voxcpm2")
AX_TIMEOUT = float(os.environ.get("AX_TIMEOUT", "300"))
AX_TTS_CHUNK_SAMPLES = int(os.environ.get("AX_TTS_CHUNK_SAMPLES", "9600"))
AX_VOICE_DIR = Path(os.environ.get(
    "AX_VOICE_DIR", Path(__file__).resolve().parents[2] / "samples" / "voices"))
_VOICE_CACHE: dict[str, dict[str, str | None]] = {}
_VOICE_CACHE_LOCK = threading.Lock()
_VOICE_REQUEST_COUNT: dict[str, int] = {}


AX_MODEL_CATALOG = {
    "asr": {
        "stage": "ASR",
        "name": "Qwen3-ASR-1.7B",
        "parameters": "1.7B",
        "description": "语音识别",
    },
    "translation": {
        "stage": "翻译",
        "name": "Hy-MT2-1.8B",
        "parameters": "1.8B",
        "description": "多语种文本翻译",
    },
    "tts": {
        "stage": "TTS",
        "name": "VoxCPM2",
        "parameters": "约 2B",
        "description": "流式语音合成 / 声音克隆",
    },
}


_HYMT2_CODES = {
    "zho_Hans": "zh", "zho_Hant": "zh", "eng_Latn": "en", "jpn_Jpan": "ja",
    "kor_Hang": "ko", "fra_Latn": "fr", "deu_Latn": "de",
    "rus_Cyrl": "ru", "spa_Latn": "es",
}
_HYMT2_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "ru": "俄语", "es": "西班牙语",
}


def validate_language_pair(src_lang: str, tgt_lang: str) -> tuple[str, str]:
    """Validate a language pair against the AX Hy-MT2 package."""
    source = _hymt2_code(normalize(src_lang))
    target = _hymt2_code(normalize(tgt_lang))
    if source == target:
        raise ValueError("source and target languages must be different")
    return source, target


def supported_languages() -> list[str]:
    return sorted(set(_HYMT2_CODES.values()))


def _reference_voice(voice: str | None) -> dict[str, str]:
    """Return cached VoxCPM2 clone fields for a VoxWeft voice id."""
    if not voice:
        return {}
    key = str(voice)
    with _VOICE_CACHE_LOCK:
        uses = _VOICE_REQUEST_COUNT[key] = _VOICE_REQUEST_COUNT.get(key, 0) + 1
        if uses in (20, 50, 100):
            logging.getLogger("voxweft.ax8860").warning(
                "reference voice %r has been used %d times; watch board RSS for allocator growth",
                key, uses)
    with _VOICE_CACHE_LOCK:
        if key in _VOICE_CACHE:
            cached = _VOICE_CACHE[key]
            return {k: v for k, v in cached.items() if v is not None} if cached else {}

    try:
        manifest = json.loads((AX_VOICE_DIR / "voices.json").read_text(encoding="utf-8"))
        item = manifest[key]
        wav_path = AX_VOICE_DIR / f"{key}.wav"
        if not wav_path.is_file():
            raise FileNotFoundError(wav_path)
        audio, sample_rate = sf.read(wav_path, dtype="float32", always_2d=False)
        if audio.ndim != 1:
            audio = audio.mean(axis=1)
        if sample_rate != 16000:
            # VoxCPM2 accepts 16 kHz reference audio. Keeping the full clip
            # preserves ref_text while reducing the per-request payload to about
            # one third of the 48 kHz seed WAV.
            audio = resample_poly(audio, 16000, sample_rate)
        wav = io.BytesIO()
        sf.write(wav, np.clip(audio, -1.0, 1.0), 16000,
                 format="WAV", subtype="PCM_16")
        value = {
            "ref_audio": base64.b64encode(wav.getvalue()).decode("ascii"),
            "ref_text": str(item.get("text", "")).strip(),
        }
        if not value["ref_text"]:
            raise ValueError(f"reference voice {key!r} has no transcript")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        _VOICE_CACHE[key] = {}
        logging.getLogger("voxweft.ax8860").warning(
            "reference voice %r unavailable; using VoxCPM2 preset (%s)", key, exc)
        return {}
    _VOICE_CACHE[key] = value
    return value


def available_voices() -> list[dict[str, object]]:
    """List seed voices without loading their audio into memory."""
    try:
        manifest = json.loads((AX_VOICE_DIR / "voices.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        {
            "id": voice_id,
            "available": (AX_VOICE_DIR / f"{voice_id}.wav").is_file(),
            "caption": item.get("caption", ""),
        }
        for voice_id, item in manifest.items()
    ]


def _post_json(base: str, payload: dict, timeout: float | None = None) -> str:
    response = requests.post(
        f"{base}/chat/completions",
        json=payload,
        timeout=timeout or AX_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"AX8860 endpoint {base}/chat/completions returned "
            f"HTTP {response.status_code}: {response.text[:500]}"
        )
    result = response.json()
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"invalid chat completion from {base}: {result}") from exc


def transcribe(audio: np.ndarray, language: str | None = None,
               hotwords=None) -> dict:
    """Transcribe a 16 kHz mono float32 segment through AX8860 Qwen3-ASR."""
    started = time.perf_counter()
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1:
        raise ValueError(f"ASR expects mono audio, got shape={audio.shape}")

    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    wav = io.BytesIO()
    sf.write(wav, pcm16, 16000, format="WAV", subtype="PCM_16")
    encoded = base64.b64encode(wav.getvalue()).decode("ascii")

    payload = {
        "model": AX_ASR_MODEL,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "input_audio",
                "input_audio": {"format": "wav", "data": encoded},
            }],
        }],
        "temperature": 0,
        "max_tokens": 512,
    }
    # These fields follow the AXERA multimodal extension used by the bridge.
    # Unknown fields are ignored by ordinary OpenAI-compatible servers.
    if language:
        payload["language"] = language
    if hotwords:
        payload["hotword"] = list(hotwords)

    content = _post_json(AX_ASR_BASE, payload)
    _, separator, text = content.partition("<asr_text>")
    text = (text if separator else content).strip()
    text = re.sub(r"^language\s+[^<\n]+", "", text, flags=re.IGNORECASE).strip()
    return {
        "text": text,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "raw": content,
    }


def _hymt2_code(code: str) -> str:
    normalized = normalize(code)
    try:
        return _HYMT2_CODES[normalized]
    except KeyError as exc:
        supported = "/".join(sorted(set(_HYMT2_CODES.values())))
        raise RuntimeError(
            f"HY-MT2 AX8860 package does not advertise {normalized}; "
            f"supported codes: {supported}"
        ) from exc


def translation_prompt(text: str, source: str, target: str) -> str:
    del source  # Kept in the public signature for engine compatibility.
    target_code = _hymt2_code(target)
    target_name = _HYMT2_NAMES[target_code]
    return (
        f"将以下文本翻译为{target_name}，"
        "注意只需要输出翻译后的结果，不要额外解释：\n\n"
        f"{text}"
    )


def _clean_translation(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^译文[:：]\s*", "", text)
    text = re.sub(r"^(?:Translation|Translated text)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


class AXHyMT2Translator:
    """OpenAI-compatible client for the AX8860 Hy-MT2 llama-server."""

    def __init__(self, model: str | None = None):
        self.model = model or AX_MT_MODEL
        self._model_ready = False

    def warmup(self) -> dict:
        response = requests.get(f"{AX_MT_BASE}/models", timeout=min(AX_TIMEOUT, 30))
        response.raise_for_status()
        self._model_ready = True
        return response.json()

    def translate(self, text: str, src_lang: str, tgt_lang: str) -> dict:
        started = time.perf_counter()
        source, target = normalize(src_lang), normalize(tgt_lang)
        if source == target:
            return {"text": text, "latency_ms": 0, "echo": True}

        # Resolve source early so unsupported language pairs fail clearly.
        _hymt2_code(source)
        user_message = {
            "role": "user",
            "content": translation_prompt(text, source, target),
        }
        payload = {
            "model": self.model,
            "messages": [user_message],
            "temperature": 0,
            "max_tokens": 512,
            "stream": False,
        }
        output = _post_json(AX_MT_BASE, payload)
        if not output.strip():
            # AXERA's llama-server build has a deterministic prompt-cache bug:
            # repeating exactly the same prompt can make the next completion
            # emit EOS immediately with empty content.  A unique system request
            # id changes tokenization without asking the model to translate it.
            # Board services should also use --no-cache-prompt; this retry is a
            # compatibility guard for older launch scripts.
            payload["messages"] = [
                {
                    "role": "system",
                    "content": (
                        f"Request ID: {uuid.uuid4().hex}. "
                        "Output only the translation."
                    ),
                },
                user_message,
            ]
            output = _post_json(AX_MT_BASE, payload)
        result = _clean_translation(output)
        if not result:
            raise RuntimeError(f"HY-MT2 returned empty translation for {text!r}")
        return {
            "text": result,
            "latency_ms": (time.perf_counter() - started) * 1000,
        }


def _sample_rate(response: requests.Response) -> int:
    content_type = response.headers.get("Content-Type", "")
    match = re.search(r"(?:rate|sample[_-]rate)\s*=\s*(\d+)", content_type, re.IGNORECASE)
    if match:
        return int(match.group(1))
    for name in ("X-Sample-Rate", "X-Audio-Sample-Rate"):
        if response.headers.get(name, "").isdigit():
            return int(response.headers[name])
    return 48000


def synthesize_stream(text: str, voice: str | None = None) -> Iterator[np.ndarray]:
    """Yield float32 mono chunks from AX8860 VoxCPM2 streamed PCM.

    VoxWeft's orchestrator expects float32 samples and converts them back to
    int16 before sending WebSocket binary frames. ``voice`` names a seed
    reference such as ``zh_tgt``; missing references fall back to the server's
    preset instead of failing the interpretation turn.
    """
    payload = {
        "model": AX_TTS_MODEL,
        "input": text,
        "response_format": "pcm",
        "stream": True,
    }
    payload.update(_reference_voice(voice))
    response = requests.post(
        f"{AX_TTS_BASE}/audio/speech",
        json=payload,
        stream=True,
        timeout=(min(AX_TIMEOUT, 30), AX_TIMEOUT),
    )
    if response.status_code >= 400:
        response.raise_for_status()

    rate = _sample_rate(response)
    if rate != 48000:
        response.close()
        raise RuntimeError(f"VoxCPM2 AX server returned unexpected sample rate {rate}")

    target_bytes = AX_TTS_CHUNK_SAMPLES * 2
    pending = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=target_bytes):
            if not chunk:
                continue
            pending.extend(chunk)
            while len(pending) >= target_bytes:
                raw = bytes(pending[:target_bytes])
                del pending[:target_bytes]
                yield np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if len(pending) >= 2:
            raw = bytes(pending[:len(pending) // 2 * 2])
            yield np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    finally:
        response.close()


def health() -> dict:
    def one(base: str) -> dict:
        try:
            response = requests.get(f"{base}/models", timeout=3)
            models = []
            if response.ok:
                try:
                    models = [item.get("id") for item in response.json().get("data", [])]
                except Exception:
                    models = []
            return {"ok": response.ok, "status": response.status_code, "models": models}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    stages = {
        "asr": one(AX_ASR_BASE),
        "translation": one(AX_MT_BASE),
        "tts": one(AX_TTS_BASE),
    }
    models = []
    for key, stage in stages.items():
        item = dict(AX_MODEL_CATALOG[key])
        item["ok"] = bool(stage.get("ok"))
        item["runtime_models"] = stage.get("models", [])
        models.append(item)

    return {
        **stages,
        "models": models,
        "voices": available_voices(),
        "resources": board_resources(),
        "endpoints": {
            "asr": AX_ASR_BASE,
            "translation": AX_MT_BASE,
            "tts": AX_TTS_BASE,
        },
    }


def board_resources() -> dict:
    """Read one tempmon snapshot for the health/resource panel."""
    try:
        response = requests.get(AX_STATS_URL, timeout=3)
        response.raise_for_status()
        data = response.json()
        mem = data.get("mem") or {}
        cmm = data.get("cmm") or {}
        llama = data.get("llama") or {}
        zones = data.get("zones") or []
        temperatures = [float(z.get("c")) for z in zones if z.get("c") is not None]
        return {
            "available": True,
            "os_total_mb": round(float(mem.get("total_kb", 0)) / 1024, 1),
            "os_used_mb": round(float(mem.get("used_kb", 0)) / 1024, 1),
            "os_available_mb": round(float(mem.get("avail_kb", 0)) / 1024, 1),
            "cmm_total_mb": round(float(cmm.get("total_kb", 0)) / 1024, 1),
            "cmm_used_mb": round(float(cmm.get("used_kb", 0)) / 1024, 1),
            "llama_process_count": llama.get("process_count"),
            "llama_rss_mb": round(float(llama.get("rss_kb", 0)) / 1024, 1),
            "npu_temp_max_c": max(temperatures) if temperatures else None,
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}
