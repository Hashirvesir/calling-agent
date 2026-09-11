"""TTS provider/model selection API.

GET  /api/tts-config          — current selection + whether each provider's key is set
GET  /api/tts-config/models   — LIVE model listing from ElevenLabs, fixed list for UpliftAI
PUT  /api/tts-config          — validate against the live/fixed listing, then save

ElevenLabs's model list is never hardcoded: its /v1/models endpoint is queried
with the platform's API key, so the dashboard only ever offers models this
account can actually call. UpliftAI has no model-variant concept, so its
listing is a fixed single-entry list gated only on whether UPLIFT_API_KEY is
set. Results are cached for 60s.
"""

import asyncio
import os
import time

import aiohttp
import websockets
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.database import get_agent_by_id, update_agent
from app.core.tts_config import get_tts_config, set_tts_config, PROVIDERS, DEFAULT_MODELS, MIN_SPEED, MAX_SPEED

router = APIRouter(tags=["tts"])

_MODELS_URL = "https://api.elevenlabs.io/v1/models"
_VOICES_URL = "https://api.elevenlabs.io/v1/voices"

_CACHE_TTL = 60.0
_models_cache: dict[str, tuple[float, list[str]]] = {}
_voices_cache: dict[str, tuple[float, list[dict]]] = {}

# UpliftAI exposes one synthesis engine, not a set of model variants — this is
# the only valid "model" value for that provider (see tts_config.py).
_UPLIFT_MODELS = [DEFAULT_MODELS["uplift"]]

# UpliftAI's voice catalogue is fixed and undocumented via any live-listing
# endpoint (same reasoning as its one-model shortcut above) — these are the
# same 4 Urdu voices app/services/tts.py's UpliftHttpTTSService.AVAILABLE_VOICES
# lists; Sindhi/Balochi are omitted here since default_language only supports
# ur/en today (see bot.py's LANGUAGE_NAMES).
UPLIFT_VOICES = [
    {"id": "v_8eelc901", "name": "Info / Edu", "description": "Clear educational tone"},
    {"id": "v_kwmp7zxt", "name": "Gen Z", "description": "Casual modern style"},
    {"id": "v_yypgzenx", "name": "Dada Jee", "description": "Traditional respectful tone"},
    {"id": "v_30s70t3a", "name": "Nostalgic News", "description": "Classic news anchor"},
]


# A well-known ElevenLabs premade voice (Rachel), available on every account
# — used only to probe whether a model accepts a WebSocket connection at all;
# never actually used for real synthesis.
_PROBE_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
_STREAM_WS_URL = "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id={model_id}"


async def _probe_elevenlabs_streaming(api_key: str, model_id: str) -> bool:
    """Whether this model actually accepts the real-time WebSocket streaming
    connection our voice pipeline depends on (ElevenLabsTTSService/pipecat).
    ElevenLabs's /v1/models listing carries no field for this — confirmed
    live: eleven_v3 and eleven_v3_conversational are TTS-capable but reject
    the streaming-input socket with a bare 403, while every turbo/flash/
    multilingual model connects fine. Selecting a model that fails this
    breaks every call's audio output outright (confirmed via a live agent-
    test session), so it must never reach the dropdown."""
    url = _STREAM_WS_URL.format(voice_id=_PROBE_VOICE_ID, model_id=model_id)
    try:
        async with websockets.connect(
            url, additional_headers={"xi-api-key": api_key}, open_timeout=8
        ):
            return True
    except Exception as exc:
        logger.warning(f"elevenlabs streaming probe for {model_id} failed: {exc}")
        return False


async def _fetch_elevenlabs_models() -> list[str] | None:
    """Return text-to-speech-capable model ids from ElevenLabs that this
    account can actually use with our real-time WebSocket pipeline, or None
    if the key is missing/invalid or the API is unreachable. Cached for 60s."""
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        return None

    cached = _models_cache.get("elevenlabs")
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _MODELS_URL,
                headers={"xi-api-key": api_key},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"elevenlabs /v1/models returned {resp.status}")
                    return None
                data = await resp.json()

        # Excludes voice-conversion-only and STT-only entries (e.g. "scribe")
        # — only models that can actually synthesize speech are candidates.
        candidates = sorted(
            m["model_id"] for m in data
            if isinstance(m, dict) and m.get("model_id") and m.get("can_do_text_to_speech")
        )
        results = await asyncio.gather(
            *(_probe_elevenlabs_streaming(api_key, m) for m in candidates)
        )
        models = [m for m, ok in zip(candidates, results) if ok]
    except Exception as exc:
        logger.warning(f"elevenlabs /v1/models unreachable: {exc}")
        return None

    _models_cache["elevenlabs"] = (time.time(), models)
    return models


def _uplift_models() -> list[str] | None:
    return _UPLIFT_MODELS if os.getenv("UPLIFT_API_KEY", "") else None


async def _fetch_elevenlabs_voices() -> list[dict] | None:
    """Return this account's actual ElevenLabs voice library (id/name/description),
    or None if the key is missing/invalid or the API is unreachable. Cached for
    60s. Unlike models, voices are account-specific — agents.voice_urdu/voice_english
    must store a real voice_id from this list for ElevenLabs to actually use it
    (see bot.py's TTS engine branch — a value that isn't a real ElevenLabs voice_id
    silently falls back to the system default voice)."""
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        return None

    cached = _voices_cache.get("elevenlabs")
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _VOICES_URL,
                headers={"xi-api-key": api_key},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"elevenlabs /v1/voices returned {resp.status}")
                    return None
                data = await resp.json()
    except Exception as exc:
        logger.warning(f"elevenlabs /v1/voices unreachable: {exc}")
        return None

    voices = [
        {
            "id": v["voice_id"],
            "name": v.get("name") or v["voice_id"],
            "description": (v.get("labels") or {}).get("description")
            or (v.get("labels") or {}).get("accent") or "",
        }
        for v in (data.get("voices") or [])
        if isinstance(v, dict) and v.get("voice_id")
    ]
    _voices_cache["elevenlabs"] = (time.time(), voices)
    return voices


@router.get("/api/tts-config")
async def get_config(user_id: str = Depends(get_current_user)):
    provider, model, speed = await get_tts_config(user_id)
    return {
        "provider": provider,
        "model": model,
        "speed": speed,
        "keys_configured": {
            "elevenlabs": bool(os.getenv("ELEVENLABS_API_KEY", "")),
            "uplift": bool(os.getenv("UPLIFT_API_KEY", "")),
        },
    }


@router.get("/api/tts-config/models")
async def list_models(user_id: str = Depends(get_current_user)):
    """Live model listing for ElevenLabs, fixed listing for UpliftAI. No key
    (or an unreachable API) reports models: null so the UI can say why."""
    return {
        "providers": {
            "elevenlabs": await _fetch_elevenlabs_models(),
            "uplift": _uplift_models(),
        }
    }


@router.get("/api/tts-config/voices")
async def list_voices(user_id: str = Depends(get_current_user)):
    """Per-provider VOICE listing — different from /models above (that's the
    synthesis engine variant, e.g. eleven_turbo_v2_5). Used by the agent
    create/edit pages' voice picker, which needs a real voice_id to store in
    agents.voice_urdu/voice_english. UpliftAI's list is fixed; ElevenLabs's
    is this account's actual voice library, live-fetched. null = key missing
    or unreachable, so the UI can explain why the picker is unavailable."""
    return {
        "providers": {
            "elevenlabs": await _fetch_elevenlabs_voices(),
            "uplift": UPLIFT_VOICES if os.getenv("UPLIFT_API_KEY", "") else None,
        }
    }


class TtsConfigUpdate(BaseModel):
    provider: str
    model: str


@router.put("/api/tts-config")
async def update_config(body: TtsConfigUpdate, user_id: str = Depends(get_current_user)):
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider: {body.provider}")

    if body.provider == "uplift":
        models = _uplift_models()
        if models is None:
            raise HTTPException(
                400,
                "UpliftAI API key is not configured on the backend — cannot select it.",
            )
    else:
        models = await _fetch_elevenlabs_models()
        if models is None:
            raise HTTPException(
                400,
                "ElevenLabs API key is not configured or the provider is "
                "unreachable — cannot verify the model.",
            )

    if body.model not in models:
        raise HTTPException(
            400,
            f"Model '{body.model}' is not available for {body.provider}. "
            f"Available: {', '.join(models)}",
        )

    try:
        await set_tts_config(user_id, body.provider, body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"provider": body.provider, "model": body.model}


@router.get("/api/agents/{agent_id}/tts-config")
async def get_agent_tts_config(agent_id: str, user_id: str = Depends(get_current_user)):
    """Per-agent TTS override, or the account default if the agent hasn't
    set one — see app/core/tts_config.py's get_tts_config for the resolution
    order."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    provider, model, speed = await get_tts_config(user_id, agent=agent)
    default_provider, default_model, default_speed = await get_tts_config(user_id)
    return {
        "provider": provider,
        "model": model,
        "speed": speed,
        "is_override": bool(agent.get("tts_provider")),
        "speed_is_override": agent.get("tts_speed") is not None,
        "account_default": {"provider": default_provider, "model": default_model, "speed": default_speed},
        "keys_configured": {
            "elevenlabs": bool(os.getenv("ELEVENLABS_API_KEY", "")),
            "uplift": bool(os.getenv("UPLIFT_API_KEY", "")),
        },
    }


class AgentTtsConfigUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    # Independent of provider/model — same reasoning as AgentLlmConfigUpdate's
    # temperature. None = no override (use the platform default, 1.0x).
    speed: float | None = None


@router.put("/api/agents/{agent_id}/tts-config")
async def set_agent_tts_config(agent_id: str, body: AgentTtsConfigUpdate, user_id: str = Depends(get_current_user)):
    """Set (or, with provider: null, clear) this agent's TTS override. A
    successful override write re-prewarms the greeting — greeting_cache is
    content-addressed by (engine, voice, model, speed, text), so a TTS or
    speed override is a guaranteed cache miss, same side effect
    PATCH /api/agents/{id} already applies when voice/language change."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    if body.speed is not None and not (MIN_SPEED <= body.speed <= MAX_SPEED):
        raise HTTPException(400, f"Speed must be between {MIN_SPEED} and {MAX_SPEED}.")

    if body.provider is None:
        updated = await update_agent(agent_id, user_id, tts_provider=None, tts_model=None, tts_speed=body.speed)
    else:
        if body.provider not in PROVIDERS:
            raise HTTPException(400, f"Unknown provider: {body.provider}")
        if body.provider == "uplift":
            models = _uplift_models()
            if models is None:
                raise HTTPException(400, "UpliftAI API key is not configured on the backend — cannot select it.")
        else:
            models = await _fetch_elevenlabs_models()
            if models is None:
                raise HTTPException(
                    400,
                    "ElevenLabs API key is not configured or the provider is "
                    "unreachable — cannot verify the model.",
                )
        if body.model not in models:
            raise HTTPException(
                400,
                f"Model '{body.model}' is not available for {body.provider}. "
                f"Available: {', '.join(models)}",
            )
        updated = await update_agent(
            agent_id, user_id, tts_provider=body.provider, tts_model=body.model, tts_speed=body.speed,
        )

    if not updated:
        raise HTTPException(404, "Agent not found or update failed")

    from app.services.bot import prewarm_agent_greeting_background
    fresh_agent = await get_agent_by_id(agent_id, user_id)
    if fresh_agent:
        prewarm_agent_greeting_background(fresh_agent)

    return {
        "provider": body.provider, "model": body.model, "speed": body.speed,
        "is_override": body.provider is not None,
    }
