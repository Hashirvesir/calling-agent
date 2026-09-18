import asyncio
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from loguru import logger

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import (
    _db,
    get_calls_list,
    get_call_id_by_ccid,
    get_call_by_id,
    get_call_metrics,
    get_turns_by_call_id,
    delete_call,
    download_recording,
    end_call_by_id,
    get_user_settings,
)

router = APIRouter(tags=["calls"])

CALLS_DIR = Path("calls")
_TRANSCRIPT_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(?:_(\w+))?\.txt$")
_LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+(USER|BOT):\s+(.*)$")


def _parse_local_transcript(path: Path) -> tuple[list[dict], dict]:
    meta = {"started": None, "call_id": None}
    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("#"):
            if "started" in line:
                meta["started"] = line.split("started", 1)[1].strip()
            if "Call ID" in line:
                meta["call_id"] = line.split(":", 1)[1].strip()
            continue
        m = _LINE_RE.match(line)
        if m:
            ts, speaker, text = m.groups()
            turns.append({"ts": ts, "speaker": speaker, "text": text})
    return turns, meta


@router.get("/api/stats")
async def api_stats(user_id: str = Depends(get_current_user)):
    calls = await get_calls_list(user_id, limit=1000)
    inbound  = sum(1 for c in calls if c.get("direction") == "inbound")
    outbound = sum(1 for c in calls if c.get("direction") == "outbound")
    active   = sum(1 for c in calls if c.get("status") in ("in_progress", "dialing"))
    return {
        "total": len(calls),
        "inbound": inbound,
        "outbound": outbound,
        "active": active,
    }


@router.get("/api/calls")
async def api_calls(user_id: str = Depends(get_current_user)):
    calls = await get_calls_list(user_id, limit=200)
    return {"calls": calls}


@router.delete("/api/calls/{call_id}", status_code=204)
async def api_delete_call(call_id: str, user_id: str = Depends(get_current_user)):
    ok = await delete_call(call_id, user_id)
    if not ok:
        raise HTTPException(404, "Call not found")


@router.post("/api/calls/{call_id}/end")
async def api_end_call(call_id: str, user_id: str = Depends(get_current_user)):
    """Terminate a live call directly from the dashboard."""
    from app.api.webhooks import _telnyx_action, _trigger_extraction_after_call

    # Check call ownership and existence
    call = await get_call_by_id(call_id, user_id)
    if not call:
        # Check if call_id is a call_control_id
        db_cid = await get_call_id_by_ccid(call_id)
        if db_cid:
            call = await get_call_by_id(db_cid, user_id)

    if not call:
        # Fallback check if user_id is empty/null in DB for this call
        db = await _db()
        if db:
            r = await db.table("calls").select("*, agents(id, name, telnyx_number)").eq("id", call_id).limit(1).execute()
            if r.data and (not r.data[0].get("user_id") or r.data[0].get("user_id") == user_id):
                call = r.data[0]

    if not call:
        raise HTTPException(404, "Call not found")

    status = call.get("status")
    if status in ("ended", "stream_failed", "answer_failed", "config_error", "machine_detected", "no_answer", "busy", "hung_up"):
        return {"ok": True, "message": "Call is already ended", "status": status}

    ccid = call.get("call_control_id")
    if ccid:
        user_row = await get_user_settings(user_id)
        telnyx_api_key = (user_row.get("telnyx_api_key") if user_row else None) or settings.telnyx_api_key
        if telnyx_api_key:
            try:
                logger.info(f"Dashboard requested hangup for ccid={ccid[:14]} by user={user_id[:8]}")
                st, body = await _telnyx_action(ccid, "hangup", telnyx_api_key)
                logger.info(f"Telnyx hangup response [{st}]: {body}")
            except Exception as exc:
                logger.warning(f"Telnyx hangup call failed for ccid={ccid[:14]}: {exc}")

        # Schedule post-call extraction
        asyncio.create_task(_trigger_extraction_after_call(ccid))

    updated = await end_call_by_id(call["id"], user_id=user_id, final_status="ended")
    return {"ok": True, "message": "Call ended successfully", "call": updated or call}


@router.get("/api/conversation/db/{call_id}")
async def api_conversation_db(call_id: str, user_id: str = Depends(get_current_user)):
    turns = await get_turns_by_call_id(call_id, user_id)
    return {
        "call_id": call_id,
        "turns": [
            {"ts": t.get("timestamp_in_call", ""), "speaker": t["speaker"], "text": t["text"]}
            for t in turns
        ],
        "turn_count": len(turns),
    }


@router.get("/api/calls/{call_id}/metrics")
async def api_call_metrics(call_id: str, user_id: str = Depends(get_current_user)):
    call = await get_call_by_id(call_id, user_id)
    if not call:
        raise HTTPException(404, "Call not found")
    metrics = await get_call_metrics(call_id, user_id) or {}

    duration_min = (call.get("duration_seconds") or 0) / 60
    prompt_tok = metrics.get("llm_prompt_tokens", 0)
    completion_tok = metrics.get("llm_completion_tokens", 0)
    uplift_chars = metrics.get("tts_uplift_characters", 0)
    eleven_chars = metrics.get("tts_elevenlabs_characters", 0)
    # Which LLM actually generated these tokens — Groq/Cerebras/Together/
    # OpenAI gpt-4o failover/OpenAI Realtime all have different rates (see
    # CallMetricsCollector._llm_provider). Legacy rows predating migration 011
    # have no value here; assume Groq, the long-standing default.
    llm_provider = metrics.get("llm_provider") or "groq"
    # Which STT actually transcribed this call — Groq/Deepgram/Together each
    # have different per-minute rates. Legacy rows predating migration 014
    # have no value here; assume Groq, the long-standing default.
    stt_provider = metrics.get("stt_provider") or "groq"

    telnyx_cost = duration_min * settings.cost_telnyx_per_minute
    telnyx_recording_cost = duration_min * settings.cost_telnyx_recording_per_minute
    if llm_provider == "openai_realtime":
        llm_cost = (
            prompt_tok / 1_000_000 * settings.cost_openai_realtime_input_per_1m
            + completion_tok / 1_000_000 * settings.cost_openai_realtime_output_per_1m
        )
        # Realtime has no separate STT stage — its audio understanding is
        # priced into the tokens above, not a per-minute transcription fee.
        stt_cost = 0.0
    elif llm_provider == "grok_voice":
        # xAI bills this per audio-minute, not per token — the prompt/
        # completion token counts pipecat reports for it (if any) aren't
        # what xAI actually bills against, so duration drives cost here
        # instead of the token-based formula the other branches use.
        llm_cost = duration_min * settings.cost_grok_voice_per_minute
        stt_cost = 0.0
    else:
        if stt_provider == "deepgram":
            stt_cost = duration_min * settings.cost_deepgram_stt_per_minute
        elif stt_provider == "together":
            stt_cost = duration_min * settings.cost_together_stt_per_minute
        else:  # groq, or unknown legacy rows
            stt_cost = duration_min * settings.cost_groq_whisper_per_minute
        if llm_provider == "openai":
            llm_cost = (
                prompt_tok / 1_000_000 * settings.cost_openai_gpt4o_input_per_1m
                + completion_tok / 1_000_000 * settings.cost_openai_gpt4o_output_per_1m
            )
        elif llm_provider == "together":
            llm_cost = (
                prompt_tok / 1_000_000 * settings.cost_together_llm_input_per_1m
                + completion_tok / 1_000_000 * settings.cost_together_llm_output_per_1m
            )
        else:  # groq, cerebras, or unknown legacy rows
            llm_cost = (
                prompt_tok / 1_000_000 * settings.cost_groq_llm_input_per_1m
                + completion_tok / 1_000_000 * settings.cost_groq_llm_output_per_1m
            )
    uplift_cost = uplift_chars * settings.cost_uplift_per_character
    eleven_cost = eleven_chars * settings.cost_elevenlabs_per_character
    total_cost = (
        telnyx_cost + telnyx_recording_cost + stt_cost + llm_cost + uplift_cost + eleven_cost
    )

    return {
        "latency": {"turns": metrics.get("turn_latencies", [])},
        "llm_provider": llm_provider,
        "stt_provider": stt_provider,
        "usage": {
            "llm_prompt_tokens": prompt_tok,
            "llm_completion_tokens": completion_tok,
            "tts_uplift_characters": uplift_chars,
            "tts_elevenlabs_characters": eleven_chars,
        },
        "cost": {
            "telnyx_usd": round(telnyx_cost, 4),
            "telnyx_recording_usd": round(telnyx_recording_cost, 4),
            "llm_usd": round(llm_cost, 4),
            "stt_usd": round(stt_cost, 4),
            "tts_uplift_usd": round(uplift_cost, 4),
            "tts_elevenlabs_usd": round(eleven_cost, 4),
            "total_usd": round(total_cost, 4),
        },
    }


@router.get("/api/conversation/{filename}")
async def api_conversation_file(filename: str, user_id: str = Depends(get_current_user)):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Bad filename")
    path = CALLS_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Transcript not found")
    turns, meta = _parse_local_transcript(path)
    return {"file": filename, "started": meta.get("started"), "call_id": meta.get("call_id"), "turns": turns, "turn_count": len(turns)}


@router.get("/calls/{filename}")
async def serve_recording(filename: str):
    # No auth — recording URLs are short-lived and filename is unguessable (call_control_id based)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Bad filename")

    audio_bytes = await download_recording(filename)
    if audio_bytes:
        return Response(
            content=audio_bytes,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    path = CALLS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/health")
async def health():
    return {"status": "ok"}
