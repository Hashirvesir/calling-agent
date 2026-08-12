"""Schema validation — ensures extracted dict matches the required field list.

Guarantees:
  - All required fields are present as keys (null if missing, never absent)
  - No extra keys leak through
  - Confidence score based on fill rate
"""

from typing import Any
from app.extraction.models import ExtractionSchema, ExtractionResult


def validate_and_normalize(
    raw_extracted: dict[str, Any],
    schema: ExtractionSchema,
    call_id: str | None,
    raw_response: str | None = None,
) -> ExtractionResult:
    """Validate extracted data against schema and return ExtractionResult."""
    field_names = schema.field_names
    cleaned: dict[str, Any] = {}
    missing: list[str] = []

    for field in field_names:
        value = raw_extracted.get(field)

        # Treat empty strings as missing
        if value == "" or value == "N/A" or value == "n/a":
            value = None

        cleaned[field] = value
        if value is None:
            missing.append(field)

    total = len(field_names)
    filled = total - len(missing)

    if filled == total:
        confidence = "high"
    elif filled >= total * 0.5:
        confidence = "partial"
    else:
        confidence = "low"

    return ExtractionResult(
        call_id=call_id,
        agent_name=schema.agent_name,
        extracted=cleaned,
        missing_fields=missing,
        confidence=confidence,
        raw_response=raw_response,
    )
