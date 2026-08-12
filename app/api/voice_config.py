from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.voice_config import get_default_urdu_voice, set_default_urdu_voice, URDU_VOICES

router = APIRouter(tags=["voice"])


@router.get("/api/voice-config")
async def get_voice_config(user_id: str = Depends(get_current_user)):
    return {
        "default_urdu_voice": get_default_urdu_voice(),
        "urdu_voices": URDU_VOICES,
    }


class VoiceUpdate(BaseModel):
    voice_id: str


@router.put("/api/voice-config")
async def update_voice_config(body: VoiceUpdate, user_id: str = Depends(get_current_user)):
    try:
        set_default_urdu_voice(body.voice_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"default_urdu_voice": body.voice_id}
