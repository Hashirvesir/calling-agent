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
from loguru import logger

from app.core.config import settings
from app.core.database import init_db, cleanup_stuck_calls
from app.core.redis_client import init_redis, close_redis

from app.api.scripts import router as scripts_router
from app.api.agents import router as agents_router
from app.api.calls import router as calls_router
from app.api.webhooks import router as webhooks_router
from app.api.extraction import router as extraction_router
from app.api.voice_config import router as voice_config_router
from app.api.settings import router as settings_router
from app.api.auth_flows import router as auth_flows_router
from app.api.billing import router as billing_router


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
app.include_router(settings_router)
app.include_router(auth_flows_router)
app.include_router(billing_router)


if __name__ == "__main__":
    logger.info(f"Starting on port {settings.port}, PUBLIC_HOST={settings.public_host!r}")
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
