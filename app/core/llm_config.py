"""Per-user LLM provider/model configuration — persists in user_settings.

Each account picks its own primary call LLM independently (Settings → AI
Model), stored on their user_settings row (columns added in migration 007).
Read fresh by bot.py at the start of every call via the caller's user_id.

Model IDs are NOT validated here against a hardcoded list — providers add and
retire models server-side, so the API layer (app/api/llm_config.py) validates
a PUT against the provider's LIVE /models listing instead.
"""

from loguru import logger

PROVIDERS = ("groq", "cerebras", "together")

DEFAULT_PROVIDER = "groq"
DEFAULT_MODEL = "openai/gpt-oss-120b"

# None = don't send a temperature override at all, let the provider use its
# own model-specific default — every provider here accepts pipecat's common
# LLMSettings.temperature field (Groq/Cerebras/Together all extend
# BaseOpenAILLMService.Settings), confirmed by reading the installed
# pipecat-ai source.
DEFAULT_TEMPERATURE = None
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0


async def get_llm_config(user_id: str, agent: dict | None = None) -> tuple[str, str, float | None]:
    """Return (provider, model, temperature) for this call's primary LLM.

    If `agent` is given (the already-fetched agent dict — pass it whenever
    the caller has it, to avoid a second DB round-trip) and it has both
    llm_provider and llm_model set, that per-agent override wins (temperature
    resolves independently — an agent can override provider/model without
    also overriding temperature, and vice versa). Otherwise falls back to the
    owning account's user_settings selection, then the platform default —
    exactly today's behavior for callers that omit `agent` (e.g. the
    account-level Settings page)."""
    from app.core.database import get_user_settings

    row = await get_user_settings(user_id) if user_id else None
    account_temperature = row.get("llm_temperature") if row else None
    temperature = agent.get("llm_temperature") if agent else None
    if temperature is None:
        temperature = account_temperature if account_temperature is not None else DEFAULT_TEMPERATURE

    agent_provider = (agent.get("llm_provider") if agent else None) or None
    agent_model = (agent.get("llm_model") if agent else None) or None
    if agent_provider in PROVIDERS and agent_model:
        return agent_provider, agent_model, temperature

    provider = (row.get("llm_provider") if row else "") or DEFAULT_PROVIDER
    model = (row.get("llm_model") if row else "") or DEFAULT_MODEL
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    return provider, model, temperature


async def set_llm_config(user_id: str, provider: str, model: str) -> None:
    from app.core.database import save_user_llm_config

    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    model = (model or "").strip()
    if not model:
        raise ValueError("Model id must not be empty")
    await save_user_llm_config(user_id, provider, model)
    logger.info(f"User {user_id[:8]}… primary call LLM → {provider} / {model}")
