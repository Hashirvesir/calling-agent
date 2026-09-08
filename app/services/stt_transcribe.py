"""One-shot (non-streaming) audio transcription — used by the dashboard's
agent-test widget to transcribe a recorded mic clip. Live calls use pipecat's
streaming STT services (see bot.py's _build_stt); this hits the same
providers' plain REST "transcribe this file" endpoints instead, since a
single browser-recorded clip doesn't need a persistent stream.
"""

import os
import time

import aiohttp
from loguru import logger

_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_TOGETHER_URL = "https://api.together.xyz/v1/audio/transcriptions"

_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=5)


async def _transcribe_groq(audio_bytes: bytes, mime: str, language: str) -> str | None:
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return None
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename="clip.webm", content_type=mime or "audio/webm")
    form.add_field("model", "whisper-large-v3")
    if language:
        form.add_field("language", language)
    form.add_field("response_format", "json")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                _GROQ_URL, data=form, headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    body_text = (await resp.text())[:300]
                    logger.warning(f"Groq transcription error {resp.status}: {body_text}")
                    return None
                data = await resp.json()
                return (data.get("text") or "").strip()
    except Exception as exc:
        logger.warning(f"Groq transcription call failed: {exc}")
        return None


async def _transcribe_deepgram(audio_bytes: bytes, mime: str, language: str) -> str | None:
    api_key = os.getenv("DEEPGRAM_API_KEY", "")
    if not api_key:
        return None
    params = {"model": "nova-3-general", "smart_format": "true", "punctuate": "true"}
    if language:
        params["language"] = language
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                _DEEPGRAM_URL,
                params=params,
                data=audio_bytes,
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": mime or "audio/webm",
                },
            ) as resp:
                if resp.status != 200:
                    body_text = (await resp.text())[:300]
                    logger.warning(f"Deepgram transcription error {resp.status}: {body_text}")
                    return None
                data = await resp.json()
                channels = (data.get("results") or {}).get("channels") or []
                alts = channels[0].get("alternatives") if channels else []
                return (alts[0].get("transcript") or "").strip() if alts else ""
    except Exception as exc:
        logger.warning(f"Deepgram transcription call failed: {exc}")
        return None


async def _transcribe_together(audio_bytes: bytes, mime: str, language: str, model: str) -> str | None:
    api_key = os.getenv("TOGETHER_API_KEY", "")
    if not api_key:
        return None
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename="clip.webm", content_type=mime or "audio/webm")
    form.add_field("model", model)
    if language:
        form.add_field("language", language)
    form.add_field("response_format", "json")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                _TOGETHER_URL, data=form, headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    body_text = (await resp.text())[:300]
                    logger.warning(f"Together transcription error {resp.status}: {body_text}")
                    return None
                data = await resp.json()
                return (data.get("text") or "").strip()
    except Exception as exc:
        logger.warning(f"Together transcription call failed: {exc}")
        return None


async def transcribe_audio(
    provider: str, audio_bytes: bytes, mime: str, language: str, model: str = "",
) -> tuple[str | None, float]:
    """Return (transcript, latency_ms). transcript is None on any failure —
    caller decides how to surface that (never crashes the test turn). model
    is only meaningful for Together (Groq/Deepgram each use one fixed model)."""
    t_start = time.monotonic()
    if provider == "deepgram":
        text = await _transcribe_deepgram(audio_bytes, mime, language)
    elif provider == "together":
        text = await _transcribe_together(audio_bytes, mime, language, model)
    else:
        text = await _transcribe_groq(audio_bytes, mime, language)
    latency_ms = (time.monotonic() - t_start) * 1000
    return text, latency_ms
