"""Voice pipeline mode selection API.

GET /api/pipeline-config  — current selection + which realtime provider keys are set
PUT /api/pipeline-config  — change mode/voice (validated against key presence)

Like stt_config.py there's no live /models listing to check a PUT against —
neither OpenAI nor xAI expose a voices-listing endpoint, so each provider's
voice list is fixed (see app/core/pipeline_config.py's REALTIME_PROVIDERS).
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.database import get_agent_by_id, update_agent
from app.core.pipeline_config import (
    DEFAULT_VOICE,
    MODES,
    REALTIME_PROVIDERS,
    get_pipeline_config,
    set_pipeline_config,
)

router = APIRouter(tags=["pipeline"])

_MODE_LABELS = {"cascaded": "Cascaded (STT + LLM + TTS)"}
_MODE_LABELS.update({mode: cfg["label"] for mode, cfg in REALTIME_PROVIDERS.items()})


@router.get("/api/pipeline-config")
async def get_config(user_id: str = Depends(get_current_user)):
    mode, voice = await get_pipeline_config(user_id)
    return {
        "mode": mode,
        "voice": voice,
        "labels": _MODE_LABELS,
        # Per-mode voice lists — the frontend only shows the voice picker for
        # whichever mode is currently selected, keyed the same as "labels".
        "voices": {m: cfg["voices"] for m, cfg in REALTIME_PROVIDERS.items()},
        "keys_configured": {
            m: bool(os.getenv(cfg["api_key_env"], "")) for m, cfg in REALTIME_PROVIDERS.items()
        },
    }


class PipelineConfigUpdate(BaseModel):
    mode: str
    voice: str = DEFAULT_VOICE


@router.put("/api/pipeline-config")
async def update_config(body: PipelineConfigUpdate, user_id: str = Depends(get_current_user)):
    if body.mode not in MODES:
        raise HTTPException(400, f"Unknown pipeline mode: {body.mode}")

    provider = REALTIME_PROVIDERS.get(body.mode)
    if provider:
        if body.voice not in provider["voices"]:
            raise HTTPException(400, f"Unknown voice for {body.mode}: {body.voice}")
        if not os.getenv(provider["api_key_env"], ""):
            raise HTTPException(
                400,
                f"{provider['api_key_env']} is not configured on the platform — cannot "
                f"select {provider['label']}.",
            )

    try:
        await set_pipeline_config(user_id, body.mode, body.voice)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"mode": body.mode, "voice": body.voice}


@router.get("/api/agents/{agent_id}/pipeline-config")
async def get_agent_pipeline_config(agent_id: str, user_id: str = Depends(get_current_user)):
    """Per-agent pipeline-mode override, or the account default if the agent
    hasn't set one — see app/core/pipeline_config.py's get_pipeline_config
    for the resolution order."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    mode, voice = await get_pipeline_config(user_id, agent=agent)
    default_mode, default_voice = await get_pipeline_config(user_id)
    return {
        "mode": mode,
        "voice": voice,
        "is_override": bool(agent.get("voice_pipeline_mode")),
        "account_default": {"mode": default_mode, "voice": default_voice},
        "labels": _MODE_LABELS,
        "voices": {m: cfg["voices"] for m, cfg in REALTIME_PROVIDERS.items()},
        "keys_configured": {
            m: bool(os.getenv(cfg["api_key_env"], "")) for m, cfg in REALTIME_PROVIDERS.items()
        },
    }


class AgentPipelineConfigUpdate(BaseModel):
    mode: str | None = None
    voice: str | None = None


@router.put("/api/agents/{agent_id}/pipeline-config")
async def set_agent_pipeline_config(agent_id: str, body: AgentPipelineConfigUpdate, user_id: str = Depends(get_current_user)):
    """Set (or, with mode: null, clear) this agent's pipeline-mode override."""
    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    if body.mode is None:
        updated = await update_agent(agent_id, user_id, voice_pipeline_mode=None, realtime_voice=None)
    else:
        if body.mode not in MODES:
            raise HTTPException(400, f"Unknown pipeline mode: {body.mode}")
        provider = REALTIME_PROVIDERS.get(body.mode)
        if provider:
            if body.voice not in provider["voices"]:
                raise HTTPException(400, f"Unknown voice for {body.mode}: {body.voice}")
            if not os.getenv(provider["api_key_env"], ""):
                raise HTTPException(
                    400,
                    f"{provider['api_key_env']} is not configured on the platform — cannot "
                    f"select {provider['label']}.",
                )
        updated = await update_agent(agent_id, user_id, voice_pipeline_mode=body.mode, realtime_voice=body.voice)

    if not updated:
        raise HTTPException(404, "Agent not found or update failed")
    return {"mode": body.mode, "voice": body.voice, "is_override": body.mode is not None}
