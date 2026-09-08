"""Per-user TTS provider/model configuration — persists in user_settings.

Each account picks its own call TTS model independently (Settings → Voice
Engine), stored on their user_settings row (columns added in migration 008).
Read fresh by bot.py at the start of every call via the caller's user_id.

Two providers: ElevenLabs (default) and UpliftAI. Uplift Orator was dropped
from the default path platform-wide after a ~19s mid-call socket stall was
observed in production (a caller hung up before a retry could recover) — see
app/services/tts.py's UpliftStreamingTTSService for the tightened timeout
(sock_read=5s, total=12s, down from the unbounded stall) and fail-fast retry
that addresses this. It's available again as an explicit opt-in per user, not
the default, so an account picks it only after verifying it on their own
agent via the test widget.

Model IDs are NOT validated here against a hardcoded list for ElevenLabs —
it adds and retires models server-side, so the API layer
(app/api/tts_config.py) validates a PUT against ElevenLabs's LIVE /v1/models
listing instead. UpliftAI has no model-variant concept (one synthesis
engine), so its "model" is a fixed sentinel value kept only for schema
symmetry with the (provider, model) shape — the actual voice choice is the
agent's own voice_urdu/voice_english field, same as ElevenLabs.
"""

from loguru import logger

PROVIDERS = ("elevenlabs", "uplift")

DEFAULT_PROVIDER = "elevenlabs"

DEFAULT_MODELS: dict[str, str] = {
    "elevenlabs": "eleven_turbo_v2_5",
    "uplift": "orator-streaming",
}

# Speaking rate. 1.0 = the provider's normal speed. Confirmed live both
# providers actually honor this: ElevenLabsTTSSettings.speed accepts 0.7-1.2
# (its own documented range); UpliftAI's /synthesis/text-to-speech accepts an
# undocumented "speed" field that measurably changes output audio duration
# (tested 0.6/1.0/1.8 — shorter audio at higher values), so its usable range
# is wider — clamp to the same 0.7-1.2 band everywhere for one consistent
# slider rather than a per-provider range that would silently do different
# things depending on which engine is selected.
DEFAULT_SPEED = 1.0
MIN_SPEED = 0.7
MAX_SPEED = 1.2


async def get_tts_config(user_id: str, agent: dict | None = None) -> tuple[str, str, float]:
    """Return (provider, model, speed) for this call's TTS.

    If `agent` is given and has both tts_provider and tts_model set, that
    per-agent override wins (speed resolves independently of provider/model —
    see get_llm_config's temperature for the same reasoning). Otherwise falls
    back to the owning account's user_settings selection, then the platform
    default — exactly today's behavior for callers that omit `agent`."""
    from app.core.database import get_user_settings

    row = await get_user_settings(user_id) if user_id else None
    account_speed = row.get("tts_speed") if row else None
    speed = agent.get("tts_speed") if agent else None
    if speed is None:
        speed = account_speed if account_speed is not None else DEFAULT_SPEED

    agent_provider = (agent.get("tts_provider") if agent else None) or None
    agent_model = (agent.get("tts_model") if agent else None) or None
    if agent_provider in PROVIDERS and agent_model:
        return agent_provider, agent_model, speed

    provider = (row.get("tts_provider") if row else "") or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    model = (row.get("tts_model") if row else "") or DEFAULT_MODELS[provider]
    return provider, model, speed


async def set_tts_config(user_id: str, provider: str, model: str) -> None:
    from app.core.database import save_user_tts_config

    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    model = (model or "").strip()
    if not model:
        raise ValueError("Model id must not be empty")
    await save_user_tts_config(user_id, provider, model)
    logger.info(f"User {user_id[:8]}… call TTS → {provider} / {model}")
