"""STT provider/model selection API.

GET  /api/stt-config          — current selection + which provider keys are set
GET  /api/stt-config/models   — LIVE audio-model listing from Together AI
PUT  /api/stt-config          — change provider (+ model for Together)

Groq and Deepgram each have exactly one fixed model (see app/core/stt_config.py's
FIXED_MODELS), so their provider name is the whole selection surface — no
listing needed. Together hosts several transcription models under one
account, so its models are queried live the same way llm_config.py queries
chat models, and the dashboard only ever offers ones this account can call.
"""

import asyncio
import io
import os
import struct
import time
import wave

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.database import get_agent_by_id, update_agent
from app.core.stt_config import (
    get_stt_config, set_stt_config, PROVIDERS, FIXED_MODELS, MIN_ENDPOINTING_MS, MAX_ENDPOINTING_MS,
)

router = APIRouter(tags=["stt"])

_PROVIDER_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "together": "TOGETHER_API_KEY",
}

_PROVIDER_LABELS = {
    "groq": "Groq Whisper (whisper-large-v3)",
    "deepgram": "Deepgram (nova-3-general)",
    "together": "Together AI",
}

_MODELS_URL = "https://api.together.xyz/v1/models"
_CACHE_TTL = 60.0
_models_cache: dict[str, tuple[float, list[str]]] = {}

# Together tags actual transcription models with type "transcribe" — its
# type "audio" is TTS (Kokoro, Cartesia, Orpheus, Rime, ...), not STT, so it
# must NOT be treated as a match. Name-pattern matching is kept as a fallback
# for entries with no "type" field. Covers Whisper, Deepgram, and NVIDIA's
# Canary/Parakeet/Nemotron ASR families, the ones known to be hosted there.
_AUDIO_NAME_HINTS = ("whisper", "canary", "parakeet", "nemotron", "-asr", "asr-")

# Together AI's API sits behind Cloudflare, which returns 403 ("error code:
# 1010") to requests carrying aiohttp's default User-Agent — a browser-like
# one is required. Confirmed live: aiohttp with this header succeeds where
# the bare default fails. Pipecat's own OpenAISTTService (openai SDK client)
# is unaffected by this — this only matters for our own direct aiohttp calls.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

_TRANSCRIBE_URL = "https://api.together.xyz/v1/audio/transcriptions"


def _make_silent_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<1600h", *([0] * 1600)))
    return buf.getvalue()


_SILENT_WAV = _make_silent_wav()


async def _probe_transcribe_model(session: aiohttp.ClientSession, api_key: str, model: str) -> bool:
    """Whether this model id is actually callable via Together's serverless
    batch /v1/audio/transcriptions endpoint (the one pipecat's OpenAISTTService
    uses). Together's /v1/models listing includes entries that its "type" and
    "pricing" fields give no reliable signal for and that 400 immediately when
    called — confirmed live: deepgram/nova-3-en, deepgram/nova-3-multi and
    deepgram/flux all need a separately-provisioned dedicated endpoint
    ("Unable to access non-serverless model"), and nvidia/nemotron-3(.5)-asr-
    streaming-0.6b only support WebSocket streaming, not this batch endpoint
    — while openai/whisper-large-v3 and nvidia/parakeet-tdt-0.6b-v3 both work
    fine. A one-off probe call with a throwaway silent clip is the only way
    to actually know, so the dashboard never offers a model that will 400 on
    every real call."""
    form = aiohttp.FormData()
    form.add_field("model", model)
    form.add_field("file", _SILENT_WAV, filename="probe.wav", content_type="audio/wav")
    try:
        async with session.post(
            _TRANSCRIBE_URL,
            data=form,
            headers={**_BROWSER_HEADERS, "Authorization": f"Bearer {api_key}"},
        ) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning(f"together STT probe for {model} failed: {exc}")
        return False


async def _fetch_together_audio_models() -> list[str] | None:
    """Return transcription-capable model ids from Together AI that this
    account can actually call, or None if the key is missing/invalid or the
    API is unreachable. Cached for 60s."""
    api_key = os.getenv("TOGETHER_API_KEY", "")
    if not api_key:
        return None

    cached = _models_cache.get("together")
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _MODELS_URL,
                headers={**_BROWSER_HEADERS, "Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"together /v1/models returned {resp.status}")
                    return None
                data = await resp.json()

        entries = data if isinstance(data, list) else data.get("data", [])
        candidates = sorted(
            m["id"] for m in entries
            if isinstance(m, dict) and m.get("id")
            and (
                m.get("type") == "transcribe"
                or (m.get("type") is None and any(h in m["id"].lower() for h in _AUDIO_NAME_HINTS))
            )
        )
        probe_timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=probe_timeout) as probe_session:
            results = await asyncio.gather(
                *(_probe_transcribe_model(probe_session, api_key, m) for m in candidates)
            )
        models = [m for m, ok in zip(candidates, results) if ok]
    except Exception as exc:
        logger.warning(f"together /v1/models unreachable: {exc}")
        return None

    _models_cache["together"] = (time.time(), models)
    return models


@router.get("/api/stt-config")
async def get_config(user_id: str = Depends(get_current_user)):
    provider, model, endpointing_ms = await get_stt_config(user_id)
    return {
        "provider": provider,
        "model": model,
        "endpointing_ms": endpointing_ms,
        "labels": _PROVIDER_LABELS,
        "keys_configured": {
            p: bool(os.getenv(_PROVIDER_KEY_ENV[p], "")) for p in PROVIDERS
        },
    }


@router.get("/api/stt-config/models")
async def list_models(user_id: str = Depends(get_current_user)):
    """Live model listing — only Together needs one (Groq/Deepgram each use
    a single fixed model, see stt_config.py's FIXED_MODELS)."""
    return {"providers": {"together": await _fetch_together_audio_models()}}


class SttConfigUpdate(BaseModel):
    provider: str
    model: str | None = None


@router.put("/api/stt-config")
async def update_config(body: SttConfigUpdate, user_id: str = Depends(get_current_user)):
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider: {body.provider}")

    if not os.getenv(_PROVIDER_KEY_ENV[body.provider], ""):
        raise HTTPException(
            400,
            f"{body.provider} API key is not configured — cannot select it.",
        )

    if body.provider not in FIXED_MODELS:
        models = await _fetch_together_audio_models()
        if models is None:
            raise HTTPException(
                400,
                "Together AI API key is not configured or the provider is "
                "unreachable — cannot verify the model.",
            )
        if not body.model or body.model not in models:
            raise HTTPException(
                400,
                f"Model '{body.model}' is not available on this Together AI "
                f"account. Available: {', '.join(models)}",
            )

    try:
        await set_stt_config(user_id, body.provider, body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"provider": body.provider, "model": body.model}


@router.get("/api/agents/{agent_id}/stt-config")
async def get_agent_stt_config(agent_id: str, user_id: str = Depends(get_current_user)):
    """Per-agent STT override, or the account default if the agent hasn't
    set one — see app/core/stt_config.py's get_stt_config for the resolution
    order."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    provider, model, endpointing_ms = await get_stt_config(user_id, agent=agent)
    default_provider, default_model, default_endpointing_ms = await get_stt_config(user_id)
    return {
        "provider": provider,
        "model": model,
        "endpointing_ms": endpointing_ms,
        "is_override": bool(agent.get("stt_provider")),
        "endpointing_is_override": agent.get("stt_endpointing_ms") is not None,
        "account_default": {
            "provider": default_provider, "model": default_model, "endpointing_ms": default_endpointing_ms,
        },
        "labels": _PROVIDER_LABELS,
        "keys_configured": {
            p: bool(os.getenv(_PROVIDER_KEY_ENV[p], "")) for p in PROVIDERS
        },
    }


class AgentSttConfigUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    # Independent of provider/model — see AgentLlmConfigUpdate.temperature.
    # None = no override (use the platform default, 1000ms). This is really
    # the pipeline's VAD stop_secs, not an STT-service setting — see
    # app/core/stt_config.py's DEFAULT_ENDPOINTING_MS comment for why it
    # lives here anyway.
    endpointing_ms: float | None = None


@router.put("/api/agents/{agent_id}/stt-config")
async def set_agent_stt_config(agent_id: str, body: AgentSttConfigUpdate, user_id: str = Depends(get_current_user)):
    """Set (or, with provider: null, clear) this agent's STT override."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    if body.endpointing_ms is not None and not (MIN_ENDPOINTING_MS <= body.endpointing_ms <= MAX_ENDPOINTING_MS):
        raise HTTPException(400, f"Endpointing must be between {MIN_ENDPOINTING_MS} and {MAX_ENDPOINTING_MS} ms.")

    if body.provider is None:
        updated = await update_agent(
            agent_id, user_id, stt_provider=None, stt_model=None, stt_endpointing_ms=body.endpointing_ms,
        )
    else:
        if body.provider not in PROVIDERS:
            raise HTTPException(400, f"Unknown provider: {body.provider}")
        if not os.getenv(_PROVIDER_KEY_ENV[body.provider], ""):
            raise HTTPException(400, f"{body.provider} API key is not configured — cannot select it.")

        model_to_save = None
        if body.provider not in FIXED_MODELS:
            models = await _fetch_together_audio_models()
            if models is None:
                raise HTTPException(
                    400,
                    "Together AI API key is not configured or the provider is "
                    "unreachable — cannot verify the model.",
                )
            if not body.model or body.model not in models:
                raise HTTPException(
                    400,
                    f"Model '{body.model}' is not available on this Together AI "
                    f"account. Available: {', '.join(models)}",
                )
            model_to_save = body.model
        updated = await update_agent(
            agent_id, user_id, stt_provider=body.provider, stt_model=model_to_save,
            stt_endpointing_ms=body.endpointing_ms,
        )

    if not updated:
        raise HTTPException(404, "Agent not found or update failed")
    return {
        "provider": body.provider, "model": body.model, "endpointing_ms": body.endpointing_ms,
        "is_override": body.provider is not None,
    }
