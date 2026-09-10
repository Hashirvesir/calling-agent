"""Per-user voice pipeline mode configuration — persists in user_settings.

Each account picks cascaded STT/LLM/TTS (default) or a speech-to-speech
realtime mode independently (Settings → Voice Pipeline Mode), stored on their
user_settings row (columns added in migration 009). Read fresh by bot.py at
the start of every call via the caller's user_id.
"""

from loguru import logger

MODES = ("cascaded", "openai_realtime", "grok_voice")
DEFAULT_MODE = "cascaded"

# Every non-cascaded mode reuses pipecat's OpenAIRealtimeLLMService unmodified
# (see app/services/bot.py) — xAI's Grok Voice Agent API documents the same
# WebSocket protocol shape OpenAI's Realtime API uses (session.update events,
# base64 audio deltas, a `?model=` query param on the connect URL, and
# `Authorization: Bearer <key>` auth — confirmed by reading pipecat's own
# _connect(), which sends nothing OpenAI-specific beyond that Bearer header).
# So swapping base_url/api_key/model per provider is sufficient; no new
# service class needed. Unverified end-to-end for Grok (no xAI API key
# available to test with when this was added) — verify empirically with a
# real XAI_API_KEY before relying on it for live calls, same as any other
# newly-added provider here.
#
# "voices" below is a hardcoded list per provider — verify against each
# provider's current docs if it goes stale. OpenAI publishes no voices-listing
# endpoint at all; xAI does (GET /v1/tts/voices), so prefer that as the source
# of truth for grok_voice — see the note on its entry for why this copy was
# taken from the published catalogue instead. Both providers also accept
# custom voice IDs cloned from reference clips, which are not listed here.
REALTIME_PROVIDERS: dict[str, dict] = {
    "openai_realtime": {
        "label": "OpenAI Realtime (speech-to-speech)",
        "base_url": "wss://api.openai.com/v1/realtime",
        "api_key_env": "OPENAI_API_KEY",
        "model": None,  # None = let pipecat's own default apply (gpt-realtime-1.5)
        "voices": ("marin", "cedar", "alloy", "verse"),
        "default_voice": "marin",
    },
    "grok_voice": {
        "label": "Grok Voice (speech-to-speech)",
        "base_url": "wss://api.x.ai/v1/realtime",
        "api_key_env": "XAI_API_KEY",
        "model": "grok-voice-latest",
        # xAI's full built-in roster (28 voices). Sourced from xAI's published
        # voice catalogue rather than the live endpoint: the authoritative list
        # is GET /v1/tts/voices, but this account's XAI_API_KEY returns 403
        # ("team doesn't have any credits or licenses yet"), so it could not be
        # confirmed against the API. Two independent published listings agreed
        # on exactly these ids, and both voices pipecat's own docs name by
        # example ("eve", "rex") appear in them. IDs are case-insensitive per
        # xAI. Re-check against GET /v1/tts/voices once the account has credits
        # — a stale id here fails the session, not just the voice.
        "voices": (
            "altair", "ara", "atlas", "aurora", "carina", "castor", "celeste",
            "cosmo", "eve", "helios", "helix", "iris", "kepler", "leo",
            "liora", "lumen", "luna", "lux", "naksh", "orion", "perseus",
            "rex", "rigel", "sal", "sirius", "ursa", "zagan", "zenith",
        ),
        "default_voice": "eve",
    },
}

DEFAULT_VOICE = REALTIME_PROVIDERS[DEFAULT_MODE]["default_voice"] if DEFAULT_MODE in REALTIME_PROVIDERS else "marin"


async def get_pipeline_config(user_id: str, agent: dict | None = None) -> tuple[str, str]:
    """Return (mode, voice) for this call's pipeline.

    If `agent` is given and has voice_pipeline_mode set, that per-agent
    override wins — cascaded mode needs no voice at all, so it's a complete
    override on its own; a realtime mode additionally needs realtime_voice
    set (a realtime mode with no voice is treated as no override, since
    there's nothing usable to build a pipeline from). Otherwise falls back to
    the owning account's user_settings selection, then the platform default —
    exactly today's behavior for callers that omit `agent`."""
    agent_mode = (agent.get("voice_pipeline_mode") if agent else None) or None
    agent_voice = (agent.get("realtime_voice") if agent else None) or None
    if agent_mode in MODES:
        agent_provider = REALTIME_PROVIDERS.get(agent_mode)
        if not agent_provider:
            return agent_mode, DEFAULT_VOICE
        if agent_voice:
            return agent_mode, agent_voice

    from app.core.database import get_user_settings

    row = await get_user_settings(user_id) if user_id else None
    mode = (row.get("voice_pipeline_mode") if row else "") or DEFAULT_MODE
    voice = (row.get("realtime_voice") if row else "") or DEFAULT_VOICE
    if mode not in MODES:
        mode = DEFAULT_MODE
    provider = REALTIME_PROVIDERS.get(mode)
    if provider and voice not in provider["voices"]:
        voice = provider["default_voice"]
    return mode, voice


async def set_pipeline_config(user_id: str, mode: str, voice: str) -> None:
    from app.core.database import save_user_pipeline_config

    if mode not in MODES:
        raise ValueError(f"Unknown pipeline mode: {mode}")
    provider = REALTIME_PROVIDERS.get(mode)
    if provider and voice not in provider["voices"]:
        raise ValueError(f"Unknown voice for {mode}: {voice}")
    await save_user_pipeline_config(user_id, mode, voice)
    logger.info(f"User {user_id[:8]}… voice pipeline → {mode} / {voice}")
