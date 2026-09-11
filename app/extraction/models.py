from typing import Any, Optional
from pydantic import BaseModel, field_validator


class FieldDefinition(BaseModel):
    """Single extraction field with optional hint for the LLM."""
    name: str
    description: Optional[str] = None
    expected_type: str = "string"  # string | number | date | boolean


class ExtractionSchema(BaseModel):
    """The dynamic schema attached to each agent/script."""
    agent_name: str
    required_fields: list[FieldDefinition]

    @classmethod
    def from_field_names(cls, agent_name: str, field_names: list[str]) -> "ExtractionSchema":
        return cls(
            agent_name=agent_name,
            required_fields=[FieldDefinition(name=f) for f in field_names],
        )

    @classmethod
    def from_raw(cls, agent_name: str, raw: list[Any]) -> "ExtractionSchema":
        """Accept either list[str] or list[dict] from DB/JSON."""
        fields = []
        for item in raw:
            if isinstance(item, str):
                fields.append(FieldDefinition(name=item))
            elif isinstance(item, dict):
                fields.append(FieldDefinition(**item))
        return cls(agent_name=agent_name, required_fields=fields)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.required_fields]


class TranscriptTurn(BaseModel):
    speaker: str   # "USER" or "BOT" / "Agent"
    text: str
    turn_index: Optional[int] = None


class ExtractionRequest(BaseModel):
    """API request body."""
    call_id: Optional[str] = None
    transcript: Optional[list[TranscriptTurn]] = None  # raw transcript if no call_id
    agent_name: str
    extraction_fields: list[Any]  # list[str] or list[dict]

    @field_validator("extraction_fields")
    @classmethod
    def must_not_be_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("extraction_fields must not be empty")
        return v


class ExtractionResult(BaseModel):
    call_id: Optional[str]
    agent_name: str
    extracted: dict[str, Any]
    missing_fields: list[str]
    confidence: str  # "high" | "partial" | "low"
    raw_response: Optional[str] = None
