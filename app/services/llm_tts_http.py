"""Plain-HTTP LLM + TTS calls for the dashboard's test tools (pipeline-test,
agent-test widget) — same endpoints, models and request shape bot.py's
pipecat services would use for a live call, so latency numbers are a
faithful preview without needing the full pipeline or a phone call.
"""

import base64
import json
import os
import re
import time
from typing import AsyncGenerator

import aiohttp
from fastapi import HTTPException
from loguru import logger

LLM_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "cerebras": "https://api.cerebras.ai/v1/chat/completions",
    "together": "https://api.together.xyz/v1/chat/completions",
}
LLM_KEY_ENV = {"groq": "GROQ_API_KEY", "cerebras": "CEREBRAS_API_KEY", "together": "TOGETHER_API_KEY"}

_LLM_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=5)
_TTS_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5)


def model_extra_params(model: str) -> dict:
    """Mirrors bot.py's _model_extra_params — same reasoning knobs, so a test
    call sends the exact request shape a live call would."""
    if "gpt-oss" in model:
        return {"reasoning_effort": "low"}
    if "qwen" in model:
        return {"reasoning_format": "hidden"}
    return {}


async def _stream_llm_deltas(
    provider: str, model: str, messages: list[dict], temperature: float | None = None,
) -> AsyncGenerator[str, None]:
    """Yield raw content deltas as the LLM streams them. Shared core for both
    run_llm_stream (collects the full reply) and stream_llm_sentences (yields
    per-sentence, for progressive TTS)."""
    api_key = os.getenv(LLM_KEY_ENV[provider], "")
    if not api_key:
        raise HTTPException(400, f"{provider} API key is not configured on the backend.")

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        **model_extra_params(model),
    }
    if temperature is not None:
        payload["temperature"] = temperature

    try:
        async with aiohttp.ClientSession(timeout=_LLM_TIMEOUT) as session:
            async with session.post(
                LLM_URLS[provider],
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    body_text = (await resp.text())[:300]
                    raise HTTPException(502, f"{provider} error {resp.status}: {body_text}")
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield delta
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"LLM call failed ({provider}/{model}): {exc}")
        raise HTTPException(502, f"Could not reach {provider}: {exc}")


async def run_llm_stream(
    provider: str, model: str, messages: list[dict], temperature: float | None = None,
) -> tuple[str, float | None, float]:
    """Stream a chat completion, return (reply_text, ttft_ms, total_ms)."""
    reply_parts: list[str] = []
    ttft_ms: float | None = None
    t_start = time.monotonic()

    async for delta in _stream_llm_deltas(provider, model, messages, temperature):
        if ttft_ms is None:
            ttft_ms = (time.monotonic() - t_start) * 1000
        reply_parts.append(delta)

    total_ms = (time.monotonic() - t_start) * 1000
    reply_text = "".join(reply_parts).strip()
    if not reply_text:
        raise HTTPException(502, f"{provider}/{model} returned an empty reply.")
    return reply_text, ttft_ms, total_ms


# Sentence boundary = Latin . ! ? or Urdu/Arabic ؟ ۔ followed by whitespace —
# same punctuation set rag.py's chunker splits on.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?؟۔]\s")
_MAX_SENTENCE_BUFFER_CHARS = 220


async def stream_llm_sentences(
    provider: str, model: str, messages: list[dict], temperature: float | None = None,
) -> AsyncGenerator[dict, None]:
    """Yield {"sentence": str} as each complete sentence streams in, then a
    final {"done": True, "ttft_ms", "total_ms", "full_text"}.

    Lets the caller start TTS on sentence 1 while the LLM is still generating
    the rest — time-to-first-spoken-audio is what makes a conversation feel
    real-time, not total reply time. A buffer cap forces a flush at the last
    word boundary if the model runs long without punctuation, so a run-on
    clause can't block audio indefinitely.
    """
    buffer = ""
    full_parts: list[str] = []
    ttft_ms: float | None = None
    t_start = time.monotonic()

    async for delta in _stream_llm_deltas(provider, model, messages, temperature):
        if ttft_ms is None:
            ttft_ms = (time.monotonic() - t_start) * 1000
        buffer += delta
        full_parts.append(delta)

        while True:
            m = _SENTENCE_BOUNDARY_RE.search(buffer)
            if m:
                cut = m.end()
            elif len(buffer) >= _MAX_SENTENCE_BUFFER_CHARS:
                last_space = buffer.rfind(" ", 0, _MAX_SENTENCE_BUFFER_CHARS)
                cut = last_space + 1 if last_space > 0 else len(buffer)
            else:
                break
            sentence = buffer[:cut].strip()
            buffer = buffer[cut:]
            if sentence:
                yield {"sentence": sentence}

    tail = buffer.strip()
    if tail:
        yield {"sentence": tail}

    total_ms = (time.monotonic() - t_start) * 1000
    full_text = "".join(full_parts).strip()
    if not full_text:
        raise HTTPException(502, f"{provider}/{model} returned an empty reply.")
    yield {"done": True, "ttft_ms": ttft_ms, "total_ms": total_ms, "full_text": full_text}


async def _run_tts_stream_elevenlabs(
    model: str, text: str, voice_id: str = "", speed: float = 1.0,
) -> tuple[str | None, float | None, float | None]:
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    voice_id = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")
    if not api_key or not voice_id:
        return None, None, None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=mp3_44100_128"
    chunks: list[bytes] = []
    ttfb_ms: float | None = None
    t_start = time.monotonic()

    payload: dict = {"text": text, "model_id": model}
    if speed != 1.0:
        payload["voice_settings"] = {"speed": speed}

    try:
        async with aiohttp.ClientSession(timeout=_TTS_TIMEOUT) as session:
            async with session.post(
                url,
                json=payload,
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    body_text = (await resp.text())[:300]
                    logger.warning(f"TTS call error {resp.status}: {body_text}")
                    return None, None, None
                async for chunk in resp.content.iter_any():
                    if ttfb_ms is None:
                        ttfb_ms = (time.monotonic() - t_start) * 1000
                    chunks.append(chunk)
    except Exception as exc:
        logger.warning(f"TTS call failed: {exc}")
        return None, None, None

    if not chunks:
        return None, None, None
    total_ms = (time.monotonic() - t_start) * 1000
    return base64.b64encode(b"".join(chunks)).decode("ascii"), ttfb_ms, total_ms


_UPLIFT_STREAM_URL = "https://api.upliftai.org/v1/synthesis/text-to-speech/stream"


async def _run_tts_stream_uplift(
    text: str, voice_id: str = "", speed: float = 1.0,
) -> tuple[str | None, float | None, float | None]:
    """UpliftAI has no model-variant concept — one synthesis engine. Requests
    MP3 output directly so the response needs no client-side transcoding
    (matches the audio/mpeg mime the dashboard already expects)."""
    api_key = os.getenv("UPLIFT_API_KEY", "")
    voice_id = voice_id or os.getenv("UPLIFT_VOICE_ID", "v_8eelc901")
    if not api_key or not voice_id:
        return None, None, None

    chunks: list[bytes] = []
    ttfb_ms: float | None = None
    t_start = time.monotonic()

    payload: dict = {"text": text, "voiceId": voice_id, "outputFormat": "MP3_22050_128"}
    if speed != 1.0:
        payload["speed"] = speed

    try:
        async with aiohttp.ClientSession(timeout=_TTS_TIMEOUT) as session:
            async with session.post(
                _UPLIFT_STREAM_URL,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    body_text = (await resp.text())[:300]
                    logger.warning(f"Uplift TTS call error {resp.status}: {body_text}")
                    return None, None, None
                async for chunk in resp.content.iter_any():
                    if ttfb_ms is None:
                        ttfb_ms = (time.monotonic() - t_start) * 1000
                    chunks.append(chunk)
    except Exception as exc:
        logger.warning(f"Uplift TTS call failed: {exc}")
        return None, None, None

    if not chunks:
        return None, None, None
    total_ms = (time.monotonic() - t_start) * 1000
    return base64.b64encode(b"".join(chunks)).decode("ascii"), ttfb_ms, total_ms


async def run_tts_stream(
    provider: str, model: str, text: str, voice_id: str = "", speed: float = 1.0,
) -> tuple[str | None, float | None, float | None]:
    """Stream synthesis from this user's selected TTS provider, return
    (audio_base64, ttfb_ms, total_ms). Returns (None, None, None) on any
    failure or missing config — TTS is a bonus preview, never worth failing
    the whole test over."""
    if provider == "uplift":
        return await _run_tts_stream_uplift(text, voice_id=voice_id, speed=speed)
    return await _run_tts_stream_elevenlabs(model, text, voice_id=voice_id, speed=speed)
