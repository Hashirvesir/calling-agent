from typing import Any, Optional
from datetime import datetime
from pydantic import BaseModel


class ScriptCreate(BaseModel):
    name: str
    content: str
    language: str = "ur"
    extraction_fields: list[Any] = []


class ScriptUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    language: Optional[str] = None
    is_active: Optional[bool] = None
    extraction_fields: Optional[list[Any]] = None


class ScriptOut(BaseModel):
    id: str
    name: str
    content: str
    language: str
    is_active: bool
    extraction_fields: list[Any] = []
    created_at: datetime
    updated_at: datetime
