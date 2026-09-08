"""LLM provider/model selection API.

GET  /api/llm-config          — current selection + which provider keys are set
GET  /api/llm-config/models   — LIVE model listing from every configured provider
PUT  /api/llm-config          — validate against the live listing, then save

The model lists are never hardcoded: each provider's /v1/models endpoint is
queried with the platform's API key, so the dashboard only ever offers models
this account can actually call. Results are cached for 60s.
"""

import asyncio
import os
import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.database import get_agent_by_id, update_agent
from app.core.llm_config import get_llm_config, set_llm_config, PROVIDERS, MIN_TEMPERATURE, MAX_TEMPERATURE

router = APIRouter(tags=["llm"])

_PROVIDER_MODELS_URL = {
    "groq": "https://api.groq.com/openai/v1/models",
    "cerebras": "https://api.cerebras.ai/v1/models",
    "together": "https://api.together.xyz/v1/models",
}

_PROVIDER_KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "together": "TOGETHER_API_KEY",
}

# Models that would break or degrade a voice call, excluded from the listing:
#   whisper/orpheus/tts     — STT/TTS models, not chat
#   guard/safeguard         — safety classifiers, not chat
#   compound                — Groq's agentic system; custom tools (end_call,
#                             check_caller_history) are not supported on it
#   allam                   — no reliable function-calling support
#   thinking                — always-on reasoning variants; the think stream
#                             would be silence (or worse, spoken) on a call
#   embedding/moderation/coder — not conversational models
_EXCLUDE_SUBSTRINGS = (
    "whisper", "orpheus", "tts", "guard", "compound", "allam",
    "thinking", "embedding", "moderation", "coder", "prompt-guard",
)

# Together-only: models that ARE actually callable (pass the live probe
# above) but manually tested unreliable for this platform's real call
# tool-calling contract (end_call + check_caller_history, no search tool —
# see app/services/bot.py's _build_end_call_tools) — RAG context was in the
# system prompt exactly as a live call sends it, real Bank Islami script/
# questions, so this isn't a synthetic mismatch. Symptom seen: empty
# response (no content, no tool call) on 3/4 plain informational questions,
# which on a live call means the caller hears dead air.
#   - moonshotai/kimi-k3        — empty response on ordinary questions
#   - zai-org/glm-5.3           — inconsistent, empty response ~half the time
#   - minimaxai/minimax-m3      — empty response on the first question tried
#   - qwen/qwen3.5-9b           — rejects this tool schema outright (400)
# Re-test before removing this list — Together's hosted weights/serving
# stack for a given model id can change without the id changing.
_KNOWN_UNRELIABLE_TOOLCALLING_TOGETHER = {
    "moonshotai/kimi-k3",
    "zai-org/glm-5.3",
    "minimaxai/minimax-m3",
    "qwen/qwen3.5-9b",
}

_CACHE_TTL = 60.0
# Together's catalog needs a live per-model probe (see _probe_together_chat_model)
# on top of the /v1/models fetch, which is much more expensive than Groq/
# Cerebras's plain listing — cached longer so a page reload doesn't re-probe
# dozens of models every minute.
_TOGETHER_CACHE_TTL = 300.0
_models_cache: dict[str, tuple[float, list[str]]] = {}

# Together AI's API sits behind Cloudflare, which returns 403 ("error code:
# 1010") to requests carrying aiohttp's default User-Agent — a browser-like
# one is required. Confirmed live: aiohttp with this header succeeds where
# the bare default fails. Pipecat's own TogetherLLMService (openai SDK client)
# is unaffected by this — this only matters for our own direct aiohttp calls.
# Harmless to send to Groq/Cerebras too, so applied to all providers here.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


def _is_chat_model(model: dict, provider: str) -> bool:
    model_id = model.get("id", "")
    low = model_id.lower()
    if any(bad in low for bad in _EXCLUDE_SUBSTRINGS):
        return False
    # Together's /v1/models entries carry a "type" (chat/image/embedding/
    # moderation/rerank/audio/...) — its catalog is far bigger and messier
    # than Groq/Cerebras's, so this is a much more reliable signal than
    # substring-matching alone. Groq/Cerebras don't send this field, so
    # absence just skips the check (the substring list above already covers
    # their smaller catalogs).
    model_type = model.get("type")
    if model_type is not None and model_type != "chat":
        return False
    # Together-only: it lists many chat models (~98 of 172 as of writing —
    # mostly "nim/..." and older base checkpoints) that require a separately
    # paid, manually-provisioned dedicated endpoint — calling them through
    # the normal serverless /chat/completions endpoint 400s immediately
    # ("Unable to access non-serverless model ... create a dedicated
    # endpoint"). Confirmed live: these entries all have pricing.input and
    # pricing.output both 0, while every callable serverless model has a
    # real per-token rate (e.g. meta-llama/Llama-3.3-70B-Instruct has no
    # pricing and always 400s; the -Turbo variant has real pricing and
    # works). Scoped to Together only — Groq's /v1/models also carries a
    # "pricing" object, but shaped differently (prompt/completion string
    # fields, not input/output), which this check would misread as
    # zero-priced and wrongly wipe out every single Groq model.
    if provider == "together":
        pricing = model.get("pricing")
        if isinstance(pricing, dict) and not pricing.get("input") and not pricing.get("output"):
            return False
        if low in _KNOWN_UNRELIABLE_TOOLCALLING_TOGETHER:
            return False
    return True


async def _probe_together_chat_model(session: aiohttp.ClientSession, api_key: str, model_id: str) -> bool:
    """Whether this model id is actually callable right now on Together's
    serverless /chat/completions endpoint. Together's /v1/models listing is
    not a reliable signal on its own — confirmed live that models with
    perfectly normal non-zero pricing (Qwen/Qwen2.5-72B-Instruct-Turbo,
    deepseek-ai/DeepSeek-V3.1, several others) still 400 with "Unable to
    access non-serverless model" or "No deployments are ready to serve this
    endpoint" — Together's catalog includes models that are listed and
    priced but not currently deployed. A live 1-token probe is the only way
    to actually know."""
    try:
        async with session.post(
            "https://api.together.xyz/v1/chat/completions",
            headers={**_BROWSER_HEADERS, "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model_id, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
        ) as resp:
            return resp.status == 200
    except Exception as exc:
        logger.warning(f"together chat-model probe for {model_id} failed: {exc}")
        return False


async def _fetch_provider_models(provider: str) -> list[str] | None:
    """Return chat-capable model ids for a provider, or None if its key is
    missing/invalid or the provider is unreachable. Cached for 60s (Groq/
    Cerebras) or 5 minutes (Together — see _TOGETHER_CACHE_TTL)."""
    api_key = os.getenv(_PROVIDER_KEY_ENV[provider], "")
    if not api_key:
        return None

    ttl = _TOGETHER_CACHE_TTL if provider == "together" else _CACHE_TTL
    cached = _models_cache.get(provider)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _PROVIDER_MODELS_URL[provider],
                headers={**_BROWSER_HEADERS, "Authorization": f"Bearer {api_key}"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"{provider} /models returned {resp.status}")
                    return None
                data = await resp.json()

            # Groq/Cerebras wrap the list as {"data": [...]} (OpenAI's shape);
            # Together returns a bare JSON array from this same endpoint —
            # handle both.
            entries = data if isinstance(data, list) else data.get("data", [])
            candidates = sorted(
                m["id"] for m in entries
                if isinstance(m, dict) and m.get("id") and _is_chat_model(m, provider)
            )

            if provider == "together":
                probe_timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=probe_timeout) as probe_session:
                    results = await asyncio.gather(
                        *(_probe_together_chat_model(probe_session, api_key, m) for m in candidates)
                    )
                models = [m for m, ok in zip(candidates, results) if ok]
            else:
                models = candidates
    except Exception as exc:
        logger.warning(f"{provider} /models unreachable: {exc}")
        return None

    _models_cache[provider] = (time.time(), models)
    return models


@router.get("/api/llm-config")
async def get_config(user_id: str = Depends(get_current_user)):
    provider, model, temperature = await get_llm_config(user_id)
    return {
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "keys_configured": {
            p: bool(os.getenv(_PROVIDER_KEY_ENV[p], "")) for p in PROVIDERS
        },
    }


@router.get("/api/llm-config/models")
async def list_models(user_id: str = Depends(get_current_user)):
    """Live model listing per provider. A provider with no key (or an
    unreachable API) reports models: null so the UI can say why."""
    result = {}
    for p in PROVIDERS:
        result[p] = await _fetch_provider_models(p)
    return {"providers": result}


class LlmConfigUpdate(BaseModel):
    provider: str
    model: str


@router.put("/api/llm-config")
async def update_config(body: LlmConfigUpdate, user_id: str = Depends(get_current_user)):
    if body.provider not in PROVIDERS:
        raise HTTPException(400, f"Unknown provider: {body.provider}")

    models = await _fetch_provider_models(body.provider)
    if models is None:
        raise HTTPException(
            400,
            f"{body.provider} API key is not configured or the provider is "
            f"unreachable — cannot verify the model.",
        )
    if body.model not in models:
        raise HTTPException(
            400,
            f"Model '{body.model}' is not available on this {body.provider} "
            f"account. Available: {', '.join(models)}",
        )

    try:
        await set_llm_config(user_id, body.provider, body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"provider": body.provider, "model": body.model}


@router.get("/api/agents/{agent_id}/llm-config")
async def get_agent_llm_config(agent_id: str, user_id: str = Depends(get_current_user)):
    """Per-agent LLM override, or the account default if the agent hasn't
    set one — see app/core/llm_config.py's get_llm_config for the resolution
    order."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    provider, model, temperature = await get_llm_config(user_id, agent=agent)
    default_provider, default_model, default_temperature = await get_llm_config(user_id)
    return {
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "is_override": bool(agent.get("llm_provider")),
        "temperature_is_override": agent.get("llm_temperature") is not None,
        "account_default": {"provider": default_provider, "model": default_model, "temperature": default_temperature},
        "keys_configured": {
            p: bool(os.getenv(_PROVIDER_KEY_ENV[p], "")) for p in PROVIDERS
        },
    }


class AgentLlmConfigUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    # Independent of provider/model — an agent can override temperature
    # without overriding provider/model, or vice versa. None = no override
    # (use the platform default), same sentinel meaning as provider: null.
    temperature: float | None = None


@router.put("/api/agents/{agent_id}/llm-config")
async def set_agent_llm_config(agent_id: str, body: AgentLlmConfigUpdate, user_id: str = Depends(get_current_user)):
    """Set (or, with provider: null, clear) this agent's LLM override.
    provider: null is the "revert to account default" sentinel — it writes
    both columns back to NULL rather than requiring a separate flag."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    if body.temperature is not None and not (MIN_TEMPERATURE <= body.temperature <= MAX_TEMPERATURE):
        raise HTTPException(400, f"Temperature must be between {MIN_TEMPERATURE} and {MAX_TEMPERATURE}.")

    if body.provider is None:
        updated = await update_agent(
            agent_id, user_id, llm_provider=None, llm_model=None, llm_temperature=body.temperature,
        )
    else:
        if body.provider not in PROVIDERS:
            raise HTTPException(400, f"Unknown provider: {body.provider}")
        models = await _fetch_provider_models(body.provider)
        if models is None:
            raise HTTPException(
                400,
                f"{body.provider} API key is not configured or the provider is "
                f"unreachable — cannot verify the model.",
            )
        if body.model not in models:
            raise HTTPException(
                400,
                f"Model '{body.model}' is not available on this {body.provider} "
                f"account. Available: {', '.join(models)}",
            )
        updated = await update_agent(
            agent_id, user_id, llm_provider=body.provider, llm_model=body.model, llm_temperature=body.temperature,
        )

    if not updated:
        raise HTTPException(404, "Agent not found or update failed")
    return {
        "provider": body.provider, "model": body.model, "temperature": body.temperature,
        "is_override": body.provider is not None,
    }
