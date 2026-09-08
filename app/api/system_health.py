"""System-level API provider health/credit status.

GET /api/system-health — live status for every system-level (.env) API key
this platform uses: OpenAI, UpliftAI, ElevenLabs, Groq, Cerebras, Together AI,
Deepgram. Telnyx is intentionally excluded — it's a per-user credential (see
CLAUDE.md), not a shared system key, so it doesn't belong on a system-wide
health page.

None of these providers except ElevenLabs (and Deepgram, conditionally)
expose a documented API to read remaining balance/credits directly — verified
against each provider's docs and by probing common REST patterns before
writing this (Together, UpliftAI, Groq, Cerebras all 404/lack such an
endpoint). So for those, this makes one minimal real request per provider
(one token / one short synthesis) and classifies the *outcome* — this is the
only way to actually know, and it's the same "ask the live API, never guess"
principle already used for model listings in llm_config.py/stt_config.py/
tts_config.py. Cached 60s so repeated page loads don't repeatedly spend
tokens/characters.
"""

import os
import time

import aiohttp
from fastapi import APIRouter, Depends
from loguru import logger

from app.core.auth import get_current_user

router = APIRouter(prefix="/api/system-health", tags=["system-health"])

_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, dict]] = {}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


def _quota_like(text: str) -> bool:
    low = text.lower()
    return any(
        kw in low
        for kw in ("insufficient_quota", "credit_balance_exhausted", "no_available_balance",
                   "no_availalble_balance", "insufficient balance", "payment_required",
                   "out of credits", "quota")
    )


async def _check_openai() -> dict:
    """Probes with one tiny embeddings call — the exact product RAG (rag.py)
    and extraction (extraction/service.py) actually spend OpenAI credits on,
    so this reflects the real thing that breaks when credits run out."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return {"status": "no_key"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "text-embedding-3-small", "input": "health check"},
            ) as resp:
                body = await resp.text()
                if resp.status == 200:
                    return {"status": "ok"}
                if resp.status == 429 and _quota_like(body):
                    return {"status": "exhausted", "detail": "insufficient_quota"}
                return {"status": "error", "detail": f"HTTP {resp.status}"}
    except Exception as exc:
        logger.warning(f"OpenAI health probe failed: {exc}")
        return {"status": "error", "detail": str(exc)}


async def _check_uplift() -> dict:
    """Probes with the shortest possible real synthesis request — UpliftAI
    has no cheaper account/usage endpoint (confirmed: /v1/account, /v1/user,
    /v1/credits, /v1/usage all 404)."""
    api_key = os.getenv("UPLIFT_API_KEY", "")
    if not api_key:
        return {"status": "no_key"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(
                "https://api.upliftai.org/v1/synthesis/text-to-speech",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"text": "a", "voiceId": "v_8eelc901", "outputFormat": "WAV_22050_16"},
            ) as resp:
                if resp.status == 200:
                    return {"status": "ok"}
                # UpliftAI's 402 body uses its own inconsistent typo of "available"
                # ("NO_AVAILALBE_BALANCE") depending on endpoint — rather than
                # chase every spelling variant, trust the 402 status alone: this
                # API only ever returns Payment Required for a balance issue.
                if resp.status == 402:
                    return {"status": "exhausted", "detail": "NO_AVAILABLE_BALANCE"}
                return {"status": "error", "detail": f"HTTP {resp.status}"}
    except Exception as exc:
        logger.warning(f"UpliftAI health probe failed: {exc}")
        return {"status": "error", "detail": str(exc)}


async def _check_elevenlabs() -> dict:
    """The one provider here with a real, free, documented usage endpoint —
    no probe request needed, just read the account's actual character quota."""
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        return {"status": "no_key"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(
                "https://api.elevenlabs.io/v1/user/subscription",
                headers={"xi-api-key": api_key},
            ) as resp:
                if resp.status != 200:
                    return {"status": "error", "detail": f"HTTP {resp.status}"}
                data = await resp.json()
                used = data.get("character_count", 0)
                limit = data.get("character_limit", 0)
                status = "exhausted" if limit and used >= limit else "ok"
                return {
                    "status": status,
                    "usage": used,
                    "limit": limit,
                    "unit": "characters",
                    "tier": data.get("tier"),
                }
    except Exception as exc:
        logger.warning(f"ElevenLabs health probe failed: {exc}")
        return {"status": "error", "detail": str(exc)}


async def _check_deepgram() -> dict:
    """Deepgram does have a real balance endpoint, but it requires the API
    key to carry the billing:read scope — most keys (including this
    project's) don't, and Deepgram has no way to grant it after the fact
    except issuing a new key. Falls back to a tiny transcription probe when
    that scope is missing, same principle as the other reactive checks."""
    api_key = os.getenv("DEEPGRAM_API_KEY", "")
    if not api_key:
        return {"status": "no_key"}
    headers = {"Authorization": f"Token {api_key}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get("https://api.deepgram.com/v1/projects", headers=headers) as resp:
                if resp.status == 200:
                    projects = (await resp.json()).get("projects", [])
                    if projects:
                        project_id = projects[0]["project_id"]
                        async with session.get(
                            f"https://api.deepgram.com/v1/projects/{project_id}/balances",
                            headers=headers,
                        ) as bal_resp:
                            if bal_resp.status == 200:
                                balances = (await bal_resp.json()).get("balances", [])
                                total = sum(float(b.get("amount", 0)) for b in balances)
                                return {
                                    "status": "exhausted" if total <= 0 else "ok",
                                    "usage": None,
                                    "limit": round(total, 4),
                                    "unit": "usd_remaining",
                                }
                            # 403 = key lacks billing:read — fall through to a live probe.
                # Either /v1/projects failed or balances needs a scope we don't have —
                # fall back to a minimal real transcription call.
                async with session.post(
                    "https://api.deepgram.com/v1/listen?model=nova-3-general",
                    headers={**headers, "Content-Type": "audio/wav"},
                    data=b"",
                ) as probe_resp:
                    body = await probe_resp.text()
                    if probe_resp.status in (200, 400):
                        # 400 here means "bad/empty audio", which only happens after
                        # auth + balance checks pass — a real balance failure is 402.
                        return {"status": "ok", "detail": "billing:read scope not available on this key"}
                    if probe_resp.status == 402 and _quota_like(body):
                        return {"status": "exhausted", "detail": "insufficient balance"}
                    return {"status": "error", "detail": f"HTTP {probe_resp.status}"}
    except Exception as exc:
        logger.warning(f"Deepgram health probe failed: {exc}")
        return {"status": "error", "detail": str(exc)}


async def _check_completions_provider(
    provider_label: str, api_key_env: str, completions_url: str, model_id: str,
    extra_headers: dict | None = None,
) -> dict:
    """Shared probe for Groq/Cerebras/Together: send a 1-token completion to
    the exact model this platform actually uses for live calls (not just any
    model the account happens to list first — Groq's own catalog includes
    org-permission-gated entries like `compound-mini` that 403 for reasons
    unrelated to balance, which a "pick the first one" probe would wrongly
    report as broken)."""
    api_key = os.getenv(api_key_env, "")
    if not api_key:
        return {"status": "no_key"}
    headers = {**(extra_headers or {}), "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(
                completions_url,
                headers=headers,
                json={"model": model_id, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            ) as resp:
                body = await resp.text()
                if resp.status == 200:
                    return {"status": "ok"}
                if resp.status in (402, 429) and _quota_like(body):
                    return {"status": "exhausted", "detail": f"HTTP {resp.status}"}
                return {"status": "error", "detail": f"HTTP {resp.status}"}
    except Exception as exc:
        logger.warning(f"{provider_label} health probe failed: {exc}")
        return {"status": "error", "detail": str(exc)}


async def _check_groq() -> dict:
    from app.core.llm_config import DEFAULT_MODEL
    return await _check_completions_provider(
        "Groq", "GROQ_API_KEY",
        "https://api.groq.com/openai/v1/chat/completions",
        DEFAULT_MODEL,
    )


async def _check_cerebras() -> dict:
    return await _check_completions_provider(
        "Cerebras", "CEREBRAS_API_KEY",
        "https://api.cerebras.ai/v1/chat/completions",
        "gpt-oss-120b",
    )


async def _check_together() -> dict:
    return await _check_completions_provider(
        "Together AI", "TOGETHER_API_KEY",
        "https://api.together.xyz/v1/chat/completions",
        "openai/gpt-oss-120b",
        extra_headers=_BROWSER_HEADERS,
    )


_CHECKS = {
    "openai": ("OpenAI", "RAG embeddings + post-call extraction + Generate/Optimize Script + LLM failover", _check_openai),
    "uplift": ("UpliftAI", "Urdu TTS voice engine (opt-in)", _check_uplift),
    "elevenlabs": ("ElevenLabs", "Default TTS voice engine", _check_elevenlabs),
    "groq": ("Groq", "Default call LLM + Whisper STT", _check_groq),
    "cerebras": ("Cerebras", "Alternative call LLM", _check_cerebras),
    "together": ("Together AI", "Alternative call LLM + STT", _check_together),
    "deepgram": ("Deepgram", "Alternative STT", _check_deepgram),
}


@router.get("")
async def get_system_health(user_id: str = Depends(get_current_user)):
    """Live status for every system-level API key. Cached 60s per provider so
    repeated dashboard loads don't repeatedly spend real tokens/characters."""
    results = {}
    now = time.time()
    for key, (label, used_for, check_fn) in _CHECKS.items():
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            result = cached[1]
        else:
            result = await check_fn()
            _cache[key] = (now, result)
        results[key] = {"label": label, "used_for": used_for, **result}
    return {"providers": results, "checked_at": now}
