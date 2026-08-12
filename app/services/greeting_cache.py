"""Pre-synthesized greeting audio cache.

The inbound greeting text is identical for every call of a given language/voice,
so we synthesize it once (via a direct HTTP call) and replay the cached raw PCM
on connect. This removes the cold TTS time-to-first-byte (~2-3.7s on a fresh
ElevenLabs websocket) from the very start of every call.

The PCM is returned at the engine's native rate; the pipeline's output transport
resamples it to the call's rate (8 kHz PCMU for Telnyx), so callers never need to
match sample rates here.
"""

import asyncio
import hashlib
from typing import Optional

import aiohttp
from loguru import logger

# Native PCM sample rate produced per engine (transport resamples downstream).
GREETING_RATE: dict[str, int] = {"elevenlabs": 16000, "uplift": 22050}

_ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000"
_UPLIFT_STREAM_URL = "https://api.upliftai.org/v1/synthesis/text-to-speech/stream"
_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5)

# Unbounded growth guard: one entry per unique (engine, voice, greeting-text)
# combo. Simple FIFO eviction (dicts preserve insertion order) once the cap is
# hit — a dropped entry just re-synthesizes on its next cache miss.
_CACHE_MAX_SIZE = 200
_cache: dict[str, bytes] = {}
_locks: dict[str, asyncio.Lock] = {}


def _key(engine: str, voice: str, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{engine}:{voice}:{digest}"


async def _synth_elevenlabs(voice: str, text: str, api_key: str, model: Optional[str],
                            session: aiohttp.ClientSession) -> bytes:
    url = _ELEVEN_URL.format(voice=voice)
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {"text": text, "model_id": model or "eleven_turbo_v2_5"}
    async with session.post(url, json=payload, headers=headers, timeout=_TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError(f"ElevenLabs {r.status}: {(await r.text())[:160]}")
        return await r.read()  # raw pcm_16000, no header


async def _synth_uplift(voice: str, text: str, api_key: str,
                        session: aiohttp.ClientSession) -> bytes:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"text": text, "voiceId": voice, "outputFormat": "WAV_22050_16"}
    async with session.post(_UPLIFT_STREAM_URL, json=payload, headers=headers, timeout=_TIMEOUT) as r:
        if r.status != 200:
            raise RuntimeError(f"Uplift {r.status}: {(await r.text())[:160]}")
        data = await r.read()
    # Strip the 44-byte WAV header → raw PCM (16-bit LE mono @ 22050).
    return data[44:] if len(data) > 44 else data


async def get_greeting_pcm(
    engine: str,
    voice_id: str,
    text: str,
    *,
    api_key: str,
    session: aiohttp.ClientSession,
    model: Optional[str] = None,
) -> Optional[tuple[bytes, int]]:
    """Return (pcm_bytes, sample_rate) for the greeting, synthesizing+caching once.

    Returns None on any failure so the caller can fall back to live TTS.
    """
    if engine not in GREETING_RATE or not (voice_id and text and api_key):
        return None

    key = _key(engine, voice_id, text)
    if key in _cache:
        return _cache[key], GREETING_RATE[engine]

    _locks.setdefault(key, asyncio.Lock())
    async with _locks[key]:
        if key in _cache:
            return _cache[key], GREETING_RATE[engine]
        try:
            if engine == "elevenlabs":
                pcm = await _synth_elevenlabs(voice_id, text, api_key, model, session)
            else:
                pcm = await _synth_uplift(voice_id, text, api_key, session)
        except Exception as exc:
            logger.warning(f"Greeting pre-synth failed ({engine}/{voice_id}): {exc}")
            return None
        if not pcm:
            return None
        if len(_cache) >= _CACHE_MAX_SIZE:
            oldest_key = next(iter(_cache))
            _cache.pop(oldest_key, None)
            _locks.pop(oldest_key, None)
        _cache[key] = pcm
        logger.info(f"Greeting cached ({engine}/{voice_id}, {len(pcm)} bytes PCM)")
        return pcm, GREETING_RATE[engine]
