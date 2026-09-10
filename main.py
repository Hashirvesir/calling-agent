"""FastAPI application entry point.

Start with:
    python main.py
    # or
    uvicorn main:app --host 0.0.0.0 --port 7860
"""

import asyncio
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import sys

from loguru import logger

# Persistent file sink — the console alone loses everything once the window
# scrolls or the process restarts, which made a live-call failure (Cerebras
# integration, TPM throttling, etc.) impossible to diagnose after the fact.
# Console logging (loguru's default stderr sink) is untouched, this only adds
# a second destination.


def _drop_system_instruction_dump(record) -> bool:
    """Keep the composed system prompt out of the logs.

    pipecat logs the fully composed system instruction at DEBUG every time a
    realtime session starts. That is the entire agent script — the bank's
    product details and whatever else the prompt carries — written out on
    every single call, to a destination handled far more casually than the
    prompt itself (tailed in terminals, copied into issues, shipped off the
    box). Only this one record is dropped rather than lowering the level:
    everything else at DEBUG is why this sink exists, and the one-way-audio
    and latency work depends on it.
    """
    return not (
        record["name"] == "pipecat.services.llm_service"
        and record["function"] == "_compose_system_instruction"
    )


# Loguru's own stderr sink is id 0 and carries no filter, so it has to be
# replaced rather than added to — otherwise the prompt still reaches the
# console. Re-added with loguru's default format so console output is
# unchanged apart from the dropped record.
logger.remove()
logger.add(
    sys.stderr,
    level="DEBUG",
    filter=_drop_system_instruction_dump,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
)
logger.add(
    "logs/backend_{time:YYYY-MM-DD}.log",
    rotation="10 MB",
    retention="14 days",
    level="DEBUG",
    filter=_drop_system_instruction_dump,
    encoding="utf-8",
    enqueue=True,  # safe to write from multiple asyncio tasks concurrently
)

from app.core.config import settings
from app.core.database import init_db, cleanup_stuck_calls
from app.core.redis_client import init_redis, close_redis

from app.api.scripts import router as scripts_router
from app.api.agents import router as agents_router
from app.api.calls import router as calls_router
from app.api.webhooks import router as webhooks_router
from app.api.extraction import router as extraction_router
from app.api.voice_config import router as voice_config_router
from app.api.llm_config import router as llm_config_router
from app.api.stt_config import router as stt_config_router
from app.api.tts_config import router as tts_config_router
from app.api.pipeline_config import router as pipeline_config_router
from app.api.pipeline_test import router as pipeline_test_router
from app.api.agent_test import router as agent_test_router
from app.api.settings import router as settings_router
from app.api.auth_flows import router as auth_flows_router
from app.api.billing import router as billing_router
from app.api.system_health import router as system_health_router


async def _periodic_cleanup():
    """Every 30 minutes, close calls stuck in in_progress/initiated for >60 min."""
    while True:
        await asyncio.sleep(30 * 60)
        await cleanup_stuck_calls(max_age_minutes=60)


async def _preload_models():
    """Instantiate the Silero VAD analyzer once at startup so the expensive
    one-time onnxruntime init (~0.8s) is paid before any call instead of on the
    first call. Each call still creates its own instance (the analyzer is
    stateful), but the shared runtime is already warm. The blocking load runs in
    a thread so the event loop isn't stalled.
    (Smart Turn is no longer used — turn detection is VAD-timeout based.)"""
    def _load():
        try:
            from pipecat.audio.vad.silero import SileroVADAnalyzer
            from pipecat.audio.vad.vad_analyzer import VADParams
            SileroVADAnalyzer(params=VADParams())
        except Exception as exc:
            logger.warning(f"VAD preload skipped: {exc}")

    try:
        await asyncio.to_thread(_load)
        logger.info("Model preload complete (VAD warm).")
    except Exception as exc:
        logger.warning(f"Model preload failed: {exc}")


async def _prewarm_rag():
    """Build each active agent's RAG embeddings AND pre-synthesize their inbound
    greeting at startup, so the first call never blocks on a cold embedding build
    (~5-8s) or a cold TTS greeting (~2-3.7s). Background — never blocks startup."""
    import aiohttp
    from app.core.database import get_all_active_agents_with_scripts
    from app.services.bot import _get_agent_rag, prewarm_agent_greeting

    try:
        agents = await get_all_active_agents_with_scripts()
    except Exception as exc:
        logger.warning(f"Prewarm: could not list agents: {exc}")
        return

    rag_built = 0
    greet_built = 0
    async with aiohttp.ClientSession() as session:
        for agent in agents:
            script = agent.get("scripts") or {}
            if (script.get("content") or "").strip():
                try:
                    if await _get_agent_rag(agent, agent.get("user_id", "")):
                        rag_built += 1
                except Exception as exc:
                    logger.warning(f"RAG prewarm failed for agent '{agent.get('name')}': {exc}")
            try:
                if await prewarm_agent_greeting(agent, session):
                    greet_built += 1
            except Exception as exc:
                logger.warning(f"Greeting prewarm failed for agent '{agent.get('name')}': {exc}")
    logger.info(f"Prewarm complete — RAG: {rag_built}, greetings: {greet_built} agent(s).")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Startup: initializing Supabase...")
    await init_db()
    await init_redis()
    await cleanup_stuck_calls(max_age_minutes=60)
    logger.info("Supabase ready — warming models, RAG and greetings in background…")
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    preload_task = asyncio.create_task(_preload_models())
    prewarm_task = asyncio.create_task(_prewarm_rag())
    yield
    cleanup_task.cancel()
    preload_task.cancel()
    prewarm_task.cancel()
    await close_redis()


app = FastAPI(title="Invenco Call Agent", lifespan=lifespan)

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scripts_router)
app.include_router(agents_router)
app.include_router(calls_router)
app.include_router(webhooks_router)
app.include_router(extraction_router)
app.include_router(voice_config_router)
app.include_router(llm_config_router)
app.include_router(stt_config_router)
app.include_router(tts_config_router)
app.include_router(pipeline_config_router)
app.include_router(pipeline_test_router)
app.include_router(agent_test_router)
app.include_router(settings_router)
app.include_router(auth_flows_router)
app.include_router(billing_router)
app.include_router(system_health_router)


if __name__ == "__main__":
    logger.info(f"Starting on port {settings.port}, PUBLIC_HOST={settings.public_host!r}")
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
