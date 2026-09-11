"""Global voice configuration — persists across requests via a JSON sidecar file."""

import json
import os
from pathlib import Path

from loguru import logger

_CONFIG_FILE = Path("voice_config.json")

URDU_VOICES = [
    {"id": "v_8eelc901", "name": "Info / Edu",       "description": "Clear educational tone"},
    {"id": "v_kwmp7zxt", "name": "Gen Z",             "description": "Casual modern style"},
    {"id": "v_yypgzenx", "name": "Dada Jee",          "description": "Traditional respectful tone"},
    {"id": "v_30s70t3a", "name": "Nostalgic News",    "description": "Classic news anchor"},
]

_VALID_IDS = {v["id"] for v in URDU_VOICES}

_default_urdu_voice: str = os.getenv("VOICE_URDU_DEFAULT", "v_8eelc901")


def _load() -> None:
    global _default_urdu_voice
    if not _CONFIG_FILE.exists():
        return
    try:
        data = json.loads(_CONFIG_FILE.read_text())
        voice = data.get("default_urdu_voice", "")
        if voice in _VALID_IDS:
            _default_urdu_voice = voice
            logger.info(f"Voice config loaded: {voice}")
    except Exception as exc:
        logger.warning(f"voice_config.json unreadable: {exc}")


_load()


def get_default_urdu_voice() -> str:
    return _default_urdu_voice


def set_default_urdu_voice(voice_id: str) -> None:
    global _default_urdu_voice
    if voice_id not in _VALID_IDS:
        raise ValueError(f"Unknown voice_id: {voice_id}")
    _default_urdu_voice = voice_id
    try:
        _CONFIG_FILE.write_text(json.dumps({"default_urdu_voice": voice_id}))
    except Exception as exc:
        logger.warning(f"Could not persist voice config: {exc}")
    logger.info(f"Default Urdu voice → {voice_id}")
