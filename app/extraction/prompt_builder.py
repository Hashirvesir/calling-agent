"""Dynamic prompt generator.

Builds a system + user prompt pair from an ExtractionSchema and a transcript.
The prompts are tuned for:
  - Roman Urdu / Urdu / English mixed transcripts
  - Strict JSON-only output (no markdown fences, no prose)
  - null for missing, never hallucinated values
"""

import json
from app.extraction.models import ExtractionSchema, TranscriptTurn

_SYSTEM_PROMPT = """\
You are a precise data extraction engine for call-center transcripts.

CRITICAL LANGUAGE RULE: Every extracted value MUST be in English only.
The transcript may be in Urdu script, Roman Urdu, or mixed languages.
You must understand all of these — but ALWAYS write the extracted value in English.
NEVER output Urdu script (ا ب پ ت ...) or Roman Urdu words in any field value.
Translate names, cities, descriptions, and all other values into English before returning.

Your ONLY job: read the conversation and extract the requested fields, \
then return a single valid JSON object.

Rules you must follow:
1. Output ONLY a valid JSON object — no markdown code fences, no prose, no explanation.
2. Extract ONLY the fields listed in the schema. Do NOT add extra keys.
3. Use JSON null (not the string "null") for any field not found in the transcript.
4. Never infer, hallucinate, or guess values. Only extract what is explicitly stated.
5. Normalize values (all in English):
   - Names: title-case English ("احمد علی" → "Ahmed Ali", "muhammad" → "Muhammad")
   - Phone numbers: digits only, no spaces/dashes
   - Budgets: number + English unit ("paanch karor" → "5 crore", "50 lakh" → "50 lakh")
   - Cities: English name ("لاہور" → "Lahore", "karachi" → "Karachi")
   - Property types: English ("ghar" → "house", "flat" → "apartment", "zameen" → "plot")
   - Dates: ISO 8601 if exact; otherwise English phrasing ("agle jumay" → "next Friday")
6. Understand Roman Urdu / Urdu — but output English only.
   Quick reference: naam=name, ghar/makaan=house, zameen/plot=plot, sheher=city,
   tarikh=date, doctor=doctor, bimari/takleef=illness, budget=budget.
7. If the user corrects themselves, use the most recent value.
"""

_USER_TEMPLATE = """\
Extract the following fields from the transcript below.

## Schema
{schema_json}

## Transcript
{transcript_text}

Return a JSON object with exactly these keys: {key_list}.
Use null for any field not present in the transcript.
IMPORTANT: All values must be in English — translate from Urdu/Roman Urdu if needed.
Return ONLY the JSON object. Nothing else."""


def build_schema_json(schema: ExtractionSchema) -> str:
    """Render the schema as a JSON object of field → description."""
    obj = {}
    for field in schema.required_fields:
        desc = field.description or _default_description(field.name)
        obj[field.name] = desc
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _default_description(field_name: str) -> str:
    """Generate a sensible description from field name when none provided."""
    readable = field_name.replace("_", " ")
    hints = {
        "customer_name": "Full name of the customer (naam)",
        "patient_name": "Full name of the patient",
        "phone_number": "Customer's phone number (digits only)",
        "city": "City or area/locality mentioned (sheher, area)",
        "budget": "Budget amount with unit (e.g. 5 crore, 50 lakh)",
        "property_type": "Type of property: house/apartment/plot/commercial (ghar/flat/zameen)",
        "visit_date": "Date the customer wants to visit (tarikh)",
        "symptoms": "Medical symptoms described by the patient (bimari, takleef)",
        "appointment_date": "Date of the appointment",
        "doctor_name": "Name of the doctor",
        "product_name": "Name of the product of interest",
        "quantity": "Quantity requested",
        "delivery_address": "Delivery address",
    }
    return hints.get(field_name, f"The {readable}")


def format_transcript(turns: list[TranscriptTurn]) -> str:
    lines = []
    for turn in turns:
        speaker = "Agent" if turn.speaker.upper() in ("BOT", "AGENT", "ASSISTANT") else "User"
        lines.append(f"{speaker}: {turn.text}")
    return "\n".join(lines)


def build_prompts(schema: ExtractionSchema, turns: list[TranscriptTurn]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) ready for the LLM."""
    schema_json = build_schema_json(schema)
    transcript_text = format_transcript(turns)
    key_list = ", ".join(schema.field_names)

    user_prompt = _USER_TEMPLATE.format(
        schema_json=schema_json,
        transcript_text=transcript_text,
        key_list=key_list,
    )
    return _SYSTEM_PROMPT, user_prompt
