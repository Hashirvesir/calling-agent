"""Smoke tests for the extraction module — run with: python tests/test_extraction.py"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.extraction.parser import parse_json_response
from app.extraction.validator import validate_and_normalize
from app.extraction.models import ExtractionSchema, TranscriptTurn
from app.extraction.prompt_builder import build_prompts


def test_parser_layer1():
    raw = '{"customer_name": "Ahmed", "city": "DHA Karachi", "budget": "5 crore", "property_type": "house", "visit_date": null}'
    result = parse_json_response(raw)
    assert result is not None
    assert result["customer_name"] == "Ahmed"
    assert result["visit_date"] is None
    print("PASS  parser layer1 — clean JSON")


def test_parser_layer2_fence():
    raw = '```json\n{"customer_name": "Ahmed", "city": "DHA Karachi", "budget": "5 crore", "property_type": "house", "visit_date": null}\n```'
    result = parse_json_response(raw)
    assert result is not None
    assert result["city"] == "DHA Karachi"
    print("PASS  parser layer2 — markdown fence stripped")


def test_parser_layer2b_prose():
    raw = 'Here is the extracted data:\n{"customer_name": "Ahmed", "city": "DHA Karachi", "budget": "5 crore", "property_type": null, "visit_date": null}'
    result = parse_json_response(raw)
    assert result is not None
    assert result["budget"] == "5 crore"
    print("PASS  parser layer2b — prose + JSON")


def test_parser_repair_trailing_comma():
    raw = '{"customer_name": "Ahmed", "city": "DHA Karachi",}'
    result = parse_json_response(raw)
    assert result is not None
    print("PASS  parser repair — trailing comma")


def test_validator_full():
    schema = ExtractionSchema.from_raw(
        "property_agent",
        ["customer_name", "city", "budget", "property_type", "visit_date"],
    )
    extracted = {
        "customer_name": "Ahmed",
        "city": "DHA Karachi",
        "budget": "5 crore",
        "property_type": "house",
        "visit_date": None,
    }
    result = validate_and_normalize(extracted, schema, call_id="test-123")
    assert result.confidence == "partial"       # 4/5 filled
    assert result.missing_fields == ["visit_date"]
    assert result.extracted["customer_name"] == "Ahmed"
    print("PASS  validator — partial confidence, missing=[visit_date]")


def test_validator_all_filled():
    schema = ExtractionSchema.from_raw(
        "property_agent",
        ["customer_name", "city", "budget"],
    )
    extracted = {"customer_name": "Ahmed", "city": "DHA Karachi", "budget": "5 crore"}
    result = validate_and_normalize(extracted, schema, call_id=None)
    assert result.confidence == "high"
    assert result.missing_fields == []
    print("PASS  validator — high confidence, no missing fields")


def test_validator_empty_string_treated_as_null():
    schema = ExtractionSchema.from_raw("test_agent", ["customer_name", "city"])
    extracted = {"customer_name": "Ahmed", "city": ""}
    result = validate_and_normalize(extracted, schema, call_id=None)
    assert result.extracted["city"] is None
    assert "city" in result.missing_fields
    print("PASS  validator — empty string treated as null")


def test_prompt_builder():
    schema = ExtractionSchema.from_raw(
        "property_agent",
        ["customer_name", "city", "budget", "property_type", "visit_date"],
    )
    turns = [
        TranscriptTurn(speaker="Agent", text="Assalamualaikum sir, aapko kis type ki property chahiye?"),
        TranscriptTurn(speaker="User", text="Mujha DHA Karachi ma 240 yard ka ghar chahiye."),
        TranscriptTurn(speaker="User", text="Budget 5 crore hai."),
        TranscriptTurn(speaker="User", text="Mera naam Ahmed hai."),
    ]
    sys_p, usr_p = build_prompts(schema, turns)
    assert "Roman Urdu" in sys_p
    assert "DHA Karachi" in usr_p
    assert "customer_name" in usr_p
    assert "visit_date" in usr_p
    print("PASS  prompt builder — system + user prompt generated correctly")


def test_doctor_schema():
    schema = ExtractionSchema.from_raw(
        "doctor_agent",
        ["patient_name", "symptoms", "appointment_date"],
    )
    extracted = {"patient_name": "Ali", "symptoms": "bukhaar aur khansi", "appointment_date": None}
    result = validate_and_normalize(extracted, schema, call_id=None)
    assert result.confidence == "partial"
    assert result.missing_fields == ["appointment_date"]
    print("PASS  doctor_agent schema — multilingual field extraction validated")


if __name__ == "__main__":
    tests = [
        test_parser_layer1,
        test_parser_layer2_fence,
        test_parser_layer2b_prose,
        test_parser_repair_trailing_comma,
        test_validator_full,
        test_validator_all_filled,
        test_validator_empty_string_treated_as_null,
        test_prompt_builder,
        test_doctor_schema,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
