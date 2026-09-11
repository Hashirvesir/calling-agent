"""Extraction API endpoints."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.extraction.models import ExtractionRequest, ExtractionResult, ExtractionSchema, TranscriptTurn
from app.extraction.service import ExtractionService

router = APIRouter(prefix="/api/extraction", tags=["extraction"])

_service = ExtractionService()


class ExtractResponse(BaseModel):
    call_id: Optional[str]
    agent_name: str
    extracted: dict[str, Any]
    missing_fields: list[str]
    confidence: str


class TestTranscriptLine(BaseModel):
    speaker: str
    text: str


class TestRequest(BaseModel):
    agent_name: str
    extraction_fields: list[Any]
    transcript: list[TestTranscriptLine]


@router.post("/extract", response_model=ExtractResponse)
async def extract_from_call(body: ExtractionRequest, user_id: str = Depends(get_current_user)):
    if not body.call_id and not body.transcript:
        raise HTTPException(400, "Provide either call_id or transcript")

    if body.call_id:
        from app.core.database import check_call_owner
        if not await check_call_owner(body.call_id, user_id):
            raise HTTPException(404, "Call not found")

    schema = ExtractionSchema.from_raw(body.agent_name, body.extraction_fields)
    raw_turns = None
    if body.transcript:
        raw_turns = [TranscriptTurn(speaker=t.speaker, text=t.text) for t in body.transcript]

    result: ExtractionResult = await _service.extract(schema=schema, call_id=body.call_id, raw_turns=raw_turns)
    return ExtractResponse(
        call_id=result.call_id,
        agent_name=result.agent_name,
        extracted=result.extracted,
        missing_fields=result.missing_fields,
        confidence=result.confidence,
    )


@router.get("/result/{call_id}", response_model=ExtractResponse)
async def get_extraction_result(call_id: str, user_id: str = Depends(get_current_user)):
    from app.core.database import get_extracted_data, check_call_owner
    if not await check_call_owner(call_id, user_id):
        raise HTTPException(404, "No extraction result found for this call")
    try:
        row = await get_extracted_data(call_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    if not row:
        raise HTTPException(404, "No extraction result found for this call")
    return ExtractResponse(
        call_id=call_id,
        agent_name=row.get("agent_name", ""),
        extracted=row.get("extracted_data", {}),
        missing_fields=row.get("missing_fields", []),
        confidence=row.get("confidence", "unknown"),
    )


@router.get("/agent/{agent_id}")
async def get_agent_extractions(agent_id: str, user_id: str = Depends(get_current_user)):
    try:
        from app.core.database import get_extracted_data_by_agent
        rows = await get_extracted_data_by_agent(agent_id, user_id)
    except Exception as exc:
        raise HTTPException(500, str(exc))

    meta_keys = {"call_id", "phone", "started_at", "duration_seconds", "confidence", "missing_count"}
    seen: dict[str, int] = {}
    for row in rows:
        for k in row:
            if k not in meta_keys:
                seen[k] = seen.get(k, 0) + 1

    extraction_columns = sorted(seen, key=lambda k: -seen[k])
    return {"extraction_columns": extraction_columns, "rows": rows}


@router.delete("/row/{call_id}", status_code=204)
async def delete_extraction_row(call_id: str, user_id: str = Depends(get_current_user)):
    from app.core.database import delete_extracted_data
    ok = await delete_extracted_data(call_id, user_id)
    if not ok:
        raise HTTPException(404, "Extraction row not found")


class UpdateFieldRequest(BaseModel):
    field: str
    value: Any


@router.patch("/row/{call_id}")
async def update_extraction_field(call_id: str, body: UpdateFieldRequest, user_id: str = Depends(get_current_user)):
    from app.core.database import update_extracted_data_field
    ok = await update_extracted_data_field(call_id, body.field, body.value, user_id)
    if not ok:
        raise HTTPException(404, "Row not found or update failed")
    return {"ok": True}


class SuggestFieldsRequest(BaseModel):
    content: str


@router.post("/suggest-fields")
async def suggest_extraction_fields(body: SuggestFieldsRequest, user_id: str = Depends(get_current_user)):
    if not body.content.strip():
        raise HTTPException(400, "content must not be empty")

    try:
        from app.extraction.service import suggest_extraction_fields_from_content
        fields = await suggest_extraction_fields_from_content(body.content)
        return {"fields": fields}
    except Exception as exc:
        raise HTTPException(500, f"Field suggestion failed: {exc}")


@router.post("/test", response_model=ExtractResponse)
async def test_extraction(body: TestRequest, user_id: str = Depends(get_current_user)):
    schema = ExtractionSchema.from_raw(body.agent_name, body.extraction_fields)
    raw_turns = [TranscriptTurn(speaker=t.speaker, text=t.text) for t in body.transcript]
    result: ExtractionResult = await _service.extract(schema=schema, call_id=None, raw_turns=raw_turns)
    return ExtractResponse(
        call_id=None,
        agent_name=result.agent_name,
        extracted=result.extracted,
        missing_fields=result.missing_fields,
        confidence=result.confidence,
    )
