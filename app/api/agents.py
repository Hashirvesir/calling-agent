"""CRUD API for agents."""

from fastapi import APIRouter, Depends, HTTPException
from app.models.agent import AgentCreate, AgentUpdate
from app.core.auth import get_current_user
from app.core.database import (
    get_all_agents, get_agent_by_id,
    create_agent, update_agent, delete_agent,
)

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("")
async def list_agents(user_id: str = Depends(get_current_user)):
    return {"agents": await get_all_agents(user_id)}


@router.get("/{agent_id}")
async def get_agent(agent_id: str, user_id: str = Depends(get_current_user)):
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return agent


@router.post("", status_code=201)
async def add_agent(body: AgentCreate, user_id: str = Depends(get_current_user)):
    agent = await create_agent(
        name=body.name,
        telnyx_number=body.telnyx_number,
        user_id=user_id,
        script_id=body.script_id,
        telnyx_app_id=body.telnyx_app_id,
        system_prompt_override=body.system_prompt_override,
        voice_urdu=body.voice_urdu,
        voice_english=body.voice_english,
        default_language=body.default_language,
        greeting_text=body.greeting_text,
    )
    if not agent:
        raise HTTPException(500, "Failed to create agent")
    return agent


@router.patch("/{agent_id}")
async def edit_agent(agent_id: str, body: AgentUpdate, user_id: str = Depends(get_current_user)):
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(400, "No fields to update")
    updated = await update_agent(agent_id, user_id, **fields)
    if not updated:
        raise HTTPException(404, "Agent not found or update failed")
    if "script_id" in fields:
        # The RAG cache is keyed by user_id:agent_id, not script_id — switching
        # which script an agent uses must drop the old cached RAG or the live
        # bot keeps answering from the previous script until a restart.
        # NOTE: per-process only — with uvicorn workers>1 this reaches just the
        # worker serving this PATCH (see the cache comment in app/services/bot.py).
        from app.services.bot import _rag_cache, prewarm_agent_rag_background
        _rag_cache.pop(f"{user_id}:{agent_id}", None)
        # Rebuild now in the background instead of waiting for the next call —
        # update_agent()'s return value has no joined script content, so re-fetch.
        fresh_agent = await get_agent_by_id(agent_id, user_id)
        if fresh_agent:
            prewarm_agent_rag_background(fresh_agent, user_id)
    if fields.keys() & {"greeting_text", "default_language", "voice_urdu", "voice_english"}:
        # greeting_cache is content-addressed by (engine, voice, model, text) —
        # any of these changing the resolved greeting is a guaranteed cache
        # miss, so re-synthesize now instead of the caller's first turn paying
        # the live-TTS fallback latency.
        from app.services.bot import prewarm_agent_greeting_background
        fresh_agent = await get_agent_by_id(agent_id, user_id)
        if fresh_agent:
            prewarm_agent_greeting_background(fresh_agent)
    return updated


@router.delete("/{agent_id}", status_code=204)
async def remove_agent(agent_id: str, user_id: str = Depends(get_current_user)):
    ok = await delete_agent(agent_id, user_id)
    if not ok:
        raise HTTPException(404, "Agent not found")
