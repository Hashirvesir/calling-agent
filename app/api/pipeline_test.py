"""One-shot pipeline test — lets a user sanity-check their currently selected
LLM + TTS models (and see real latency) from the dashboard, without placing an
actual phone call.

POST /api/pipeline-test  { message: str }

Hits the exact same provider/model this user's calls use (per-user
llm_config / tts_config — see app/services/bot.py) via app/services/llm_tts_http,
so the latency numbers are a faithful preview of what a caller would experience.

STT isn't exercised here — the test input is typed text, not audio. For a
full STT+RAG+LLM+TTS test against one specific agent's script, see
app/api/agent_test.py instead.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.llm_config import get_llm_config
from app.core.tts_config import get_tts_config
from app.services.llm_tts_http import run_llm_stream, run_tts_stream

router = APIRouter(tags=["pipeline-test"])

_TEST_SYSTEM_PROMPT = (
    "You are a helpful voice assistant being sanity-checked by its operator. "
    "Reply in 1-3 short sentences, exactly as you would on a live phone call."
)


class PipelineTestRequest(BaseModel):
    message: str


@router.post("/api/pipeline-test")
async def test_pipeline(body: PipelineTestRequest, user_id: str = Depends(get_current_user)):
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(400, "message must not be empty")

    llm_provider, llm_model, llm_temperature = await get_llm_config(user_id)
    tts_provider, tts_model, tts_speed = await get_tts_config(user_id)

    messages = [
        {"role": "system", "content": _TEST_SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    reply_text, llm_ttft_ms, llm_total_ms = await run_llm_stream(llm_provider, llm_model, messages, llm_temperature)
    audio_b64, tts_ttfb_ms, tts_total_ms = await run_tts_stream(tts_provider, tts_model, reply_text, speed=tts_speed)

    return {
        "reply": reply_text,
        "llm": {
            "provider": llm_provider,
            "model": llm_model,
            "ttft_ms": round(llm_ttft_ms) if llm_ttft_ms is not None else None,
            "total_ms": round(llm_total_ms),
        },
        "tts": {
            "provider": tts_provider,
            "model": tts_model,
            "ttfb_ms": round(tts_ttfb_ms) if tts_ttfb_ms is not None else None,
            "total_ms": round(tts_total_ms) if tts_total_ms is not None else None,
            "audio_base64": audio_b64,
            "audio_mime": "audio/mpeg" if audio_b64 else None,
        },
    }
