"""Core extraction service — orchestrates the full pipeline.

Pipeline:
  1. Load transcript (from DB by call_id, or accept raw turns)
  2. Build dynamic prompt from schema + transcript
  3. Call LLM (OpenAI gpt-4o by default, one retry on JSON parse failure)
  4. Parse JSON with 3-layer fallback parser
  5. Validate and normalize against schema
  6. Persist result to DB (fire-and-forget)
  7. Return ExtractionResult
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger
from openai import AsyncOpenAI

from app.core.config import settings
from app.extraction.models import (
    ExtractionResult,
    ExtractionSchema,
    TranscriptTurn,
)
from app.extraction.parser import build_retry_prompt, parse_json_response
from app.extraction.prompt_builder import build_prompts
from app.extraction.validator import validate_and_normalize

_LLM_MODEL = "gpt-4o"
_LLM_TEMPERATURE = 0.0   # deterministic — we want exact extraction, not creativity
_MAX_TOKENS = 1024


_service_instance: "ExtractionService | None" = None


def get_extraction_service() -> "ExtractionService":
    global _service_instance
    if _service_instance is None:
        _service_instance = ExtractionService()
    return _service_instance


class ExtractionService:
    def __init__(self):
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def extract(
        self,
        schema: ExtractionSchema,
        call_id: Optional[str] = None,
        raw_turns: Optional[list[TranscriptTurn]] = None,
    ) -> ExtractionResult:
        turns = await self._load_turns(call_id, raw_turns)
        if not turns:
            logger.warning(f"ExtractionService: no transcript turns for call_id={call_id}")
            return _empty_result(schema, call_id)

        system_prompt, user_prompt = build_prompts(schema, turns)

        raw_response = await self._call_llm(system_prompt, user_prompt)
        extracted = parse_json_response(raw_response)

        # Layer 3: retry if JSON parsing failed
        if extracted is None:
            logger.warning("ExtractionService: JSON parse failed, retrying with fix prompt")
            retry_prompt = build_retry_prompt(raw_response, schema.field_names)
            raw_response = await self._call_llm(system_prompt, retry_prompt)
            extracted = parse_json_response(raw_response)

        if extracted is None:
            logger.error(f"ExtractionService: extraction failed after retry for call_id={call_id}")
            return _empty_result(schema, call_id, raw_response=raw_response)

        result = validate_and_normalize(extracted, schema, call_id, raw_response)

        if call_id:
            await self._persist(call_id, schema.agent_name, result)

        return result

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        response = await self._client.chat.completions.create(
            model=_LLM_MODEL,
            temperature=_LLM_TEMPERATURE,
            max_tokens=_MAX_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""

    async def _load_turns(
        self,
        call_id: Optional[str],
        raw_turns: Optional[list[TranscriptTurn]],
    ) -> list[TranscriptTurn]:
        if raw_turns:
            return raw_turns
        if call_id:
            return await _fetch_turns_from_db(call_id)
        return []

    async def _persist(self, call_id: str, agent_name: str, result: ExtractionResult) -> None:
        try:
            from app.core.database import save_extracted_data
            await save_extracted_data(
                call_id=call_id,
                agent_name=agent_name,
                extracted=result.extracted,
                missing_fields=result.missing_fields,
                confidence=result.confidence,
            )
        except Exception as exc:
            logger.error(f"ExtractionService: DB persist failed: {exc}")


async def _fetch_turns_from_db(call_id: str) -> list[TranscriptTurn]:
    try:
        from app.core.database import get_turns_by_call_id
        rows = await get_turns_by_call_id(call_id)
        return [
            TranscriptTurn(
                speaker=row["speaker"],
                text=row["text"],
                turn_index=row.get("turn_index"),
            )
            for row in rows
        ]
    except Exception as exc:
        logger.error(f"ExtractionService: failed to load turns from DB: {exc}")
        return []


def _empty_result(
    schema: ExtractionSchema,
    call_id: Optional[str],
    raw_response: Optional[str] = None,
) -> ExtractionResult:
    return ExtractionResult(
        call_id=call_id,
        agent_name=schema.agent_name,
        extracted={f: None for f in schema.field_names},
        missing_fields=schema.field_names,
        confidence="low",
        raw_response=raw_response,
    )
