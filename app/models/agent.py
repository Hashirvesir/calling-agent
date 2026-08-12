from typing import Optional
from datetime import datetime
from pydantic import BaseModel


class AgentCreate(BaseModel):
    name: str
    telnyx_number: str
    script_id: Optional[str] = None
    telnyx_app_id: Optional[str] = None
    system_prompt_override: Optional[str] = None
    voice_urdu: str = "v_8eelc901"
    voice_english: str = "v_8eelc901"
    default_language: str = "ur"


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    script_id: Optional[str] = None
    telnyx_number: Optional[str] = None
    telnyx_app_id: Optional[str] = None
    system_prompt_override: Optional[str] = None
    voice_urdu: Optional[str] = None
    voice_english: Optional[str] = None
    default_language: Optional[str] = None
    is_active: Optional[bool] = None


class AgentOut(BaseModel):
    id: str
    name: str
    telnyx_number: str
    script_id: Optional[str] = None
    telnyx_app_id: Optional[str] = None
    system_prompt_override: Optional[str] = None
    voice_urdu: str
    voice_english: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
