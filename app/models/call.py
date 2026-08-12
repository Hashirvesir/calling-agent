from typing import Optional
from datetime import datetime
from pydantic import BaseModel


class CallOut(BaseModel):
    id: str
    call_control_id: Optional[str] = None
    agent_id: Optional[str] = None
    direction: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    recording_storage_path: Optional[str] = None
    turn_count: int = 0
    detected_language: Optional[str] = None
    created_at: datetime


class TurnOut(BaseModel):
    id: str
    call_id: str
    speaker: str
    text: str
    turn_index: int
    timestamp_in_call: Optional[str] = None
    created_at: datetime
