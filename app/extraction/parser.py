"""Robust JSON parser with three fallback layers.

Layer 1: Direct json.loads — fastest, works when LLM is well-behaved.
Layer 2: Regex extraction — strips markdown fences / preamble text.
Layer 3: Structured retry — asks the LLM to fix its own broken output.
"""

import json
import re
from typing import Any, Optional


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def parse_json_response(raw: str) -> Optional[dict[str, Any]]:
    """Try to extract a JSON object from raw LLM output.

    Returns a dict on success, None if all layers fail.
    """
    raw = raw.strip()

    # Layer 1: direct parse
    result = _try_parse(raw)
    if result is not None:
        return result

    # Layer 2a: strip markdown code fences
    fence_match = _CODE_FENCE_RE.search(raw)
    if fence_match:
        result = _try_parse(fence_match.group(1).strip())
        if result is not None:
            return result

    # Layer 2b: find first {...} block
    obj_match = _JSON_OBJECT_RE.search(raw)
    if obj_match:
        result = _try_parse(obj_match.group(0))
        if result is not None:
            return result

    # Layer 2c: attempt basic repairs (trailing commas, single quotes)
    repaired = _repair(raw)
    result = _try_parse(repaired)
    if result is not None:
        return result

    return None


def _try_parse(text: str) -> Optional[dict[str, Any]]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _repair(text: str) -> str:
    """Apply simple heuristic repairs to common LLM JSON mistakes."""
    # Strip any leading/trailing non-JSON text before the first {
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]

    # Replace Python None/True/False with JSON equivalents
    text = re.sub(r"\bNone\b", "null", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)

    # Remove trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # Replace single-quoted strings with double-quoted (naive, but covers simple cases)
    text = re.sub(r"'([^']*)'", r'"\1"', text)

    return text


def build_retry_prompt(broken_output: str, field_names: list[str]) -> str:
    """Return a prompt that asks the model to fix its own broken JSON."""
    keys = json.dumps(field_names)
    return (
        f"Your previous response was not valid JSON. "
        f"Fix it and return ONLY a valid JSON object with these keys: {keys}.\n"
        f"Use null for missing fields. No markdown, no prose.\n\n"
        f"Your broken output was:\n{broken_output}"
    )
