"""Per-user STT provider/model configuration — persists in user_settings.

Each account picks its own call STT engine independently (Settings → STT
Engine), stored on their user_settings row (columns added in migration 007,
model added in migration 013). Read fresh by bot.py at the start of every call
via the caller's user_id.

Groq and Deepgram each use exactly one fixed model, chosen for a documented
reason (see bot.py's _build_stt): Groq → whisper-large-v3 (better Urdu
word-error rate than turbo), Deepgram → nova-3-general (the only Deepgram
tier with Urdu support) — no live /models listing to validate against for
either, since neither exposes one the provider name doesn't already fully
determine. Together AI hosts multiple transcription models (Whisper and
others) under one account, so its model IS user-selectable and live-listed,
same shape as llm_config.py's chat models.
"""

from loguru import logger

PROVIDERS = ("groq", "deepgram", "together")

DEFAULT_PROVIDER = "groq"

FIXED_MODELS: dict[str, str] = {
    "groq": "whisper-large-v3",
    "deepgram": "nova-3-general",
}

DEFAULT_TOGETHER_MODEL = "openai/whisper-large-v3"

# "Endpointing sensitivity" — how long a caller must stay silent before the
# call considers their turn finished. This isn't actually an STT-provider
# setting (none of Groq/Deepgram/Together's STT services own this) — it's
# the pipeline's own Silero VAD stop_secs (see app/services/bot.py's
# _VAD_STOP_SECS and the CNIC-number-splitting fix it exists for), grouped
# under "STT" in the UI because that's where a caller expects to find
# "how patiently does it listen" even though the mechanism lives one layer
# below the STT service itself. Stored/returned in ms (matching the
# reference UI's "300ms" style) and converted to seconds at the VAD call site.
DEFAULT_ENDPOINTING_MS = 1000.0
MIN_ENDPOINTING_MS = 300.0
MAX_ENDPOINTING_MS = 2500.0


async def get_stt_config(user_id: str, agent: dict | None = None) -> tuple[str, str, float]:
    """Return (provider, model, endpointing_ms) for this call's primary STT.

    If `agent` is given and has stt_provider set, that per-agent override
    wins — FIXED_MODELS providers (groq, deepgram) need only the provider to
    count as an override, mirroring how set_stt_config never sends a model
    for them either. endpointing_ms resolves independently of provider/model
    (same reasoning as get_llm_config's temperature). Otherwise falls back to
    the owning account's user_settings selection, then the platform default —
    exactly today's behavior for callers that omit `agent`."""
    from app.core.database import get_user_settings

    row = await get_user_settings(user_id) if user_id else None
    account_endpointing = row.get("stt_endpointing_ms") if row else None
    endpointing_ms = agent.get("stt_endpointing_ms") if agent else None
    if endpointing_ms is None:
        endpointing_ms = account_endpointing if account_endpointing is not None else DEFAULT_ENDPOINTING_MS

    agent_provider = (agent.get("stt_provider") if agent else None) or None
    if agent_provider in PROVIDERS:
        if agent_provider in FIXED_MODELS:
            return agent_provider, FIXED_MODELS[agent_provider], endpointing_ms
        agent_model = (agent.get("stt_model") if agent else None) or None
        if agent_model:
            return agent_provider, agent_model, endpointing_ms

    provider = (row.get("stt_provider") if row else "") or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    if provider in FIXED_MODELS:
        return provider, FIXED_MODELS[provider], endpointing_ms
    model = (row.get("stt_model") if row else "") or DEFAULT_TOGETHER_MODEL
    return provider, model, endpointing_ms


async def set_stt_config(user_id: str, provider: str, model: str | None = None) -> None:
    from app.core.database import save_user_stt_config

    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")

    model_to_save: str | None = None
    if provider not in FIXED_MODELS:
        model = (model or "").strip()
        if not model:
            raise ValueError("Model id must not be empty")
        model_to_save = model

    await save_user_stt_config(user_id, provider, model_to_save)
    logger.info(
        f"User {user_id[:8]}… primary call STT → {provider}"
        + (f" / {model_to_save}" if model_to_save else "")
    )
