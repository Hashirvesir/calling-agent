"""Live-conversation test widget — talk to one of your own agents (voice or
text) using its real system prompt + RAG script + your configured LLM/STT/TTS
models, without dialing an actual Telnyx call.

POST /api/agent-test/start  { agent_id }
    -> { session_id, agent_name, greeting_text, greeting_audio_base64, greeting_audio_mime }
POST /api/agent-test/turn   { session_id, text? , audio_base64?, audio_mime? }
    -> newline-delimited JSON stream:
       {"type": "chunk", "text", "audio_base64", "audio_mime"}  — one per sentence
       {"type": "done", "transcript", "reply", "dev": {...}}    — final
       {"type": "error", "detail"}                              — only if a mid-stream call failed
POST /api/agent-test/end    { session_id }
    -> { ok: true }
WS   /api/agent-test/ws?agent_id=...&token=...
    -> genuinely real-time: runs the actual run_bot() pipeline (proper Silero
       VAD, streaming STT/LLM/TTS) over a plain browser WebSocket instead of
       Telnyx. See app/services/browser_ws_serializer.py. This is the path
       the "Talk To Your Agent" button uses; the HTTP endpoints above remain
       for the text-only fallback and are turn-based (record → transcribe →
       reply → synthesize → play), which can approximate but never truly
       match real-time no matter how much that request/response cycle is
       tuned — every step is a discrete round trip instead of one continuous
       stream.

The turn endpoint streams because a live call answers sentence-by-sentence —
TTS starts on sentence 1 while the LLM is still generating the rest, instead
of the caller waiting for the whole reply to finish before hearing anything.
Returning one big JSON blob at the end (an even earlier version of this file)
made every turn feel like a long, silent pause followed by a wall of speech —
nothing like a real conversation.

Reuses exactly what a live call would (see app/services/bot.py): the agent's
system_prompt_override (or the same fallback prompt), build_static_system_messages
for the LANGUAGE/NUMBER/CONVERSATION-FLOW rules, _get_agent_rag for script
retrieval, and the user's own llm_config/stt_config/tts_config selections —
so a test session's answers, latency and RAG grounding match what a real
caller would get.

Sessions live in-process memory — a dev/test tool, not call-critical state
(same tradeoff as greeting_cache.py). Idle sessions are purged lazily.
"""

import asyncio
import base64
import json
import os
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.runner.types import WebSocketRunnerArguments
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from app.core.auth import decode_user_id, get_current_user
from app.core.database import get_agent_by_id
from app.core.llm_config import get_llm_config
from app.core.pipeline_config import REALTIME_PROVIDERS, get_pipeline_config
from app.core.stt_config import get_stt_config
from app.core.tts_config import get_tts_config
from app.services.bot import (
    _FALLBACK_PROMPT,
    _get_agent_rag,
    LANGUAGE_WHISPER_MAP,
    build_static_system_messages,
    resolve_inbound_greeting,
    run_bot,
)
from app.services.browser_ws_serializer import SAMPLE_RATE, BrowserFrameSerializer
from app.services.llm_tts_http import run_tts_stream, stream_llm_sentences
from app.services.rag import _format_target_fields, build_conv_state_message, build_rag_message, strip_rag_and_conv_messages
from app.services.stt_transcribe import transcribe_audio

router = APIRouter(tags=["agent-test"])

_SESSION_TTL_SECS = 30 * 60
_sessions: dict[str, dict] = {}


def _purge_stale_sessions() -> None:
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v["last_active"] > _SESSION_TTL_SECS]:
        _sessions.pop(sid, None)


class StartRequest(BaseModel):
    agent_id: str


@router.post("/api/agent-test/start")
async def start_session(body: StartRequest, user_id: str = Depends(get_current_user)):
    _purge_stale_sessions()

    agent = await get_agent_by_id(body.agent_id, user_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    default_lang = agent.get("default_language") or "ur"
    system_prompt = agent.get("system_prompt_override") or _FALLBACK_PROMPT
    messages, lang_name = build_static_system_messages(system_prompt, default_lang, [])

    script_cfg = agent.get("scripts") or {}
    target_fields = _format_target_fields(script_cfg.get("extraction_fields") or [])

    # Prewarm RAG in the background — first turn awaits the same cached build
    # (or the cache hit, if this finishes first) instead of blocking here.
    asyncio.create_task(_get_agent_rag(agent, user_id))

    engine, voice, _api_key, greet_model, greeting_text, greet_speed = await resolve_inbound_greeting(agent)
    audio_b64, _ttfb, _total = await run_tts_stream(engine, greet_model, greeting_text, voice_id=voice, speed=greet_speed)

    session_id = uuid.uuid4().hex
    _sessions[session_id] = {
        "user_id": user_id,
        "agent": agent,
        "default_lang": default_lang,
        "lang_name": lang_name,
        "target_fields": target_fields,
        "voice": voice,
        "messages": messages + [{"role": "assistant", "content": greeting_text}],
        "last_active": time.time(),
    }

    return {
        "session_id": session_id,
        "agent_name": agent.get("name"),
        "greeting_text": greeting_text,
        "greeting_audio_base64": audio_b64,
        "greeting_audio_mime": "audio/mpeg" if audio_b64 else None,
    }


class TurnRequest(BaseModel):
    session_id: str
    text: str | None = None
    audio_base64: str | None = None
    audio_mime: str | None = None


@router.post("/api/agent-test/turn")
async def turn(body: TurnRequest, user_id: str = Depends(get_current_user)):
    session = _sessions.get(body.session_id)
    if not session or session["user_id"] != user_id:
        raise HTTPException(404, "Test session not found or expired — start a new conversation.")
    session["last_active"] = time.time()

    agent = session["agent"]
    default_lang = session["default_lang"]
    dev: dict = {}

    if body.audio_base64:
        stt_provider, stt_model, _stt_endpointing_ms = await get_stt_config(user_id, agent=agent)
        audio_bytes = base64.b64decode(body.audio_base64)
        whisper_lang = LANGUAGE_WHISPER_MAP.get(default_lang, "ur")
        transcript, stt_latency_ms = await transcribe_audio(
            stt_provider, audio_bytes, body.audio_mime or "audio/webm", whisper_lang, stt_model,
        )
        dev["stt"] = {"provider": stt_provider, "latency_ms": round(stt_latency_ms), "transcript": transcript}
        if not transcript:
            raise HTTPException(422, "Could not transcribe that — try speaking again or type your message.")
        user_text = transcript
    elif body.text and body.text.strip():
        user_text = body.text.strip()
        dev["stt"] = None
    else:
        raise HTTPException(400, "Provide either text or audio_base64.")

    # Strip any RAG/conv-state messages left from the previous turn, append
    # this turn, then inject fresh ones for the outgoing request only — the
    # persisted history stays clean, exactly like the live-call pipeline.
    session["messages"] = strip_rag_and_conv_messages(session["messages"])
    session["messages"].append({"role": "user", "content": user_text})
    outgoing = list(session["messages"])

    conv_state_msg = build_conv_state_message(session["target_fields"], session["lang_name"])
    if conv_state_msg:
        outgoing.insert(1, conv_state_msg)

    rag = await _get_agent_rag(agent, user_id)
    if rag and rag.loaded:
        t0 = time.monotonic()
        context_text = await rag.retrieve(user_text, top_k=3)
        rag_latency_ms = (time.monotonic() - t0) * 1000
        rag_msg = build_rag_message(context_text, session["lang_name"])
        injected = False
        if rag_msg:
            for i in range(len(outgoing) - 1, -1, -1):
                if outgoing[i].get("role") == "user":
                    outgoing.insert(i, rag_msg)
                    injected = True
                    break
        dev["rag"] = {
            "loaded": True,
            "chunk_count": rag.chunk_count,
            "context_injected": injected,
            "latency_ms": round(rag_latency_ms),
            "context_preview": context_text[:400] if context_text else "",
        }
    else:
        dev["rag"] = {"loaded": False}

    llm_provider, llm_model, llm_temperature = await get_llm_config(user_id, agent=agent)
    tts_provider, tts_model, tts_speed = await get_tts_config(user_id, agent=agent)
    voice_id = session["voice"]
    had_audio_input = bool(body.audio_base64)

    async def event_stream() -> AsyncGenerator[str, None]:
        tts_ttfb_first: float | None = None
        tts_total_sum = 0.0
        tts_any = False
        try:
            async for item in stream_llm_sentences(llm_provider, llm_model, outgoing, llm_temperature):
                if "sentence" in item:
                    sentence = item["sentence"]
                    audio_b64, ttfb_ms, seg_total_ms = await run_tts_stream(
                        tts_provider, tts_model, sentence, voice_id=voice_id, speed=tts_speed,
                    )
                    if audio_b64:
                        tts_any = True
                        if tts_ttfb_first is None:
                            tts_ttfb_first = ttfb_ms
                        if seg_total_ms:
                            tts_total_sum += seg_total_ms
                    yield json.dumps({
                        "type": "chunk",
                        "text": sentence,
                        "audio_base64": audio_b64,
                        "audio_mime": "audio/mpeg" if audio_b64 else None,
                    }) + "\n"
                else:
                    reply_text = item["full_text"]
                    session["messages"].append({"role": "assistant", "content": reply_text})
                    dev["llm"] = {
                        "provider": llm_provider,
                        "model": llm_model,
                        "ttft_ms": round(item["ttft_ms"]) if item["ttft_ms"] is not None else None,
                        "total_ms": round(item["total_ms"]),
                    }
                    dev["tts"] = {
                        "provider": tts_provider,
                        "model": tts_model,
                        "ttfb_ms": round(tts_ttfb_first) if tts_ttfb_first is not None else None,
                        "total_ms": round(tts_total_sum) if tts_any else None,
                    }
                    yield json.dumps({
                        "type": "done",
                        "transcript": user_text if had_audio_input else None,
                        "reply": reply_text,
                        "dev": dev,
                    }) + "\n"
        except HTTPException as exc:
            yield json.dumps({"type": "error", "detail": str(exc.detail)}) + "\n"
        except Exception as exc:
            logger.error(f"agent-test turn stream failed: {exc}")
            yield json.dumps({"type": "error", "detail": "Something went wrong generating that reply."}) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


class EndRequest(BaseModel):
    session_id: str


@router.post("/api/agent-test/end")
async def end_session(body: EndRequest, user_id: str = Depends(get_current_user)):
    session = _sessions.get(body.session_id)
    if session and session["user_id"] == user_id:
        _sessions.pop(body.session_id, None)
    return {"ok": True}


@router.websocket("/api/agent-test/ws")
async def agent_test_ws(websocket: WebSocket):
    """Real-time voice test — runs the actual run_bot() pipeline against a
    browser WebSocket instead of Telnyx. db_call_id/call_control_id/
    hangup_callback/caller_phone are all left None/empty on purpose: this is
    not a real call, so nothing gets written to the calls/transcript_turns/
    call_metrics tables and there's no Telnyx hangup to invoke — ConversationLogger
    and CallMetricsCollector both no-op when db_call_id is falsy (see their
    own __init__/flush methods), and end_call_handler already guards
    hangup_callback being None.

    Browsers can't set an Authorization header on the WebSocket constructor,
    so the JWT travels as a query param instead of the usual header.
    """
    agent_id = websocket.query_params.get("agent_id", "")
    token = websocket.query_params.get("token", "")
    user_id = decode_user_id(token) if token else None

    if not agent_id or not user_id:
        await websocket.close(code=1008)
        return

    agent = await get_agent_by_id(agent_id, user_id)
    if not agent:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    # OpenAI Realtime needs a fixed 24kHz sample rate (the only rate its PCM
    # audio format accepts) — everything else here still uses the browser
    # widget's normal 16kHz. run_bot() re-reads pipeline_mode itself too; this
    # is just so the transport/serializer and the frontend's audio setup agree
    # with whatever run_bot() is about to build.
    pipeline_mode, _ = await get_pipeline_config(user_id, agent=agent)
    _realtime_provider = REALTIME_PROVIDERS.get(pipeline_mode)
    is_realtime = _realtime_provider is not None and bool(os.getenv(_realtime_provider["api_key_env"]))
    sample_rate = 24000 if is_realtime else SAMPLE_RATE
    # Resolved here (not the hardcoded _VAD_STOP_SECS) so this agent's own
    # endpointing-sensitivity override (Model Config → Speech recognition)
    # actually takes effect in the test widget, same as a live call.
    _, _, endpointing_ms = await get_stt_config(user_id, agent=agent)

    # Tell the frontend which rate to use before any audio flows — it has no
    # other way to learn this ahead of starting mic capture/playback.
    await websocket.send_text(json.dumps({"event": "session_info", "sample_rate": sample_rate}))

    serializer = BrowserFrameSerializer()
    params = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=sample_rate,
        audio_out_sample_rate=sample_rate,
        add_wav_header=False,
        # Silero only supports 16000/8000Hz — skip it at 24kHz (realtime mode)
        # and let OpenAI's own server-side VAD drive barge-in instead (see
        # bot.py's realtime session config).
        vad_analyzer=None if is_realtime else SileroVADAnalyzer(params=VADParams(stop_secs=endpointing_ms / 1000)),
        serializer=serializer,
    )
    transport = FastAPIWebsocketTransport(websocket=websocket, params=params)

    runner_args = WebSocketRunnerArguments(websocket=websocket)
    runner_args.handle_sigint = False

    try:
        await run_bot(
            transport,
            runner_args,
            agent=agent,
            user_id=user_id,
            browser_event_dedup=serializer.dedup,
        )
    except Exception as exc:
        logger.error(f"agent-test websocket pipeline failed: {exc}")
