#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.observers.loggers.debug_log_observer import DebugLogObserver, FrameEndpoint
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.service_switcher import ServiceSwitcher, ServiceSwitcherStrategyFailover
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.services.call_metrics_collector import CallMetricsCollector
from app.services.conversation_logger import ConversationLogger
from app.services.greeting_cache import get_greeting_pcm
from app.services.rag import RAGContextInjector, ScriptRAG
from app.services.tts import UpliftStreamingTTSService
from app.core.voice_config import get_default_urdu_voice

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Transport configs
# ---------------------------------------------------------------------------

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        # 0.6 s silence — enough pause to complete Urdu phrases without cutting mid-word
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.6)),
    ),
    "telnyx": lambda: FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.6)),
    ),
}

# ---------------------------------------------------------------------------
# Fallback prompt — used only when agent has no system_prompt_override
# and no script assigned. Agents should always have a script configured.
# ---------------------------------------------------------------------------

_FALLBACK_PROMPT = (
    "You are a polite AI assistant. "
    "No script has been configured for this agent yet. "
    "Greet the caller warmly, apologize that the service is not yet set up, "
    "and politely ask them to call back later. "
    "Then say goodbye and call end_call."
)

# ---------------------------------------------------------------------------
# Voice IDs per language — loaded from environment variables
# ---------------------------------------------------------------------------

# Urdu voices (4 style variants available)
VOICE_URDU_DEFAULT  = os.getenv("VOICE_URDU_DEFAULT",  "v_8eelc901")
VOICE_URDU_GEN_Z    = os.getenv("VOICE_URDU_GEN_Z",    "v_kwmp7zxt")
VOICE_URDU_DADA_JEE = os.getenv("VOICE_URDU_DADA_JEE", "v_yypgzenx")
VOICE_URDU_NEWS     = os.getenv("VOICE_URDU_NEWS",     "v_30s70t3a")

VOICE_ENGLISH = os.getenv("VOICE_ENGLISH", "v_8eelc901")

# ElevenLabs — English TTS. Urdu uses UpliftAI Orator; English uses ElevenLabs.
# Voice/engine is chosen once per call from the agent's locked default_language.
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL    = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")

# Static inbound greeting per language — pre-synthesizable (see greeting_cache).
INBOUND_GREETINGS: dict[str, str] = {
    "ur":  "السلام علیکم! آپ کا شکریہ کال کرنے کا۔ میں آپ کی کیا مدد کر سکتا ہوں؟",
    "en":  "Hello! Thank you for calling. How can I help you today?",
}


def resolve_inbound_greeting(agent: dict | None) -> tuple[str, str, str, str | None, str]:
    """Return (engine, voice_id, api_key, model, text) for an agent's inbound
    greeting. Engine follows the locked default_language: English → ElevenLabs,
    everything else → UpliftAI Orator. Used by both the live call and startup
    prewarm so the two never drift."""
    default_lang = (agent.get("default_language") or "ur") if agent else "ur"
    text = INBOUND_GREETINGS.get(default_lang, INBOUND_GREETINGS["ur"])
    if default_lang == "en":
        voice = (agent.get("voice_english") if agent else "") or ""
        # Legacy rows stored an Uplift "v_..." id here — fall back to the system voice.
        if not voice or voice.startswith("v_"):
            voice = ELEVENLABS_VOICE_ID
        return "elevenlabs", voice, ELEVENLABS_API_KEY, ELEVENLABS_MODEL, text
    voice = (agent.get("voice_urdu") if agent else None) or get_default_urdu_voice()
    return "uplift", voice, os.getenv("UPLIFT_API_KEY", ""), None, text


async def prewarm_agent_greeting(agent: dict, session) -> bool:
    """Pre-synthesize and cache an agent's inbound greeting. Returns True on success."""
    engine, voice, api_key, model, text = resolve_inbound_greeting(agent)
    res = await get_greeting_pcm(engine, voice, text, api_key=api_key, session=session, model=model)
    return res is not None

# Maps detected language code → TTS voice ID
LANGUAGE_VOICE_MAP: dict[str, str] = {
    "en":  VOICE_ENGLISH,
    "ur":  VOICE_URDU_DEFAULT,
}

# Human-readable language names — used to instruct the LLM which language to
# reply in. The agent's `default_language` is authoritative: the bot speaks
# ONLY this language for the whole call, regardless of what the caller uses.
LANGUAGE_NAMES: dict[str, str] = {
    "en":  "English",
    "ur":  "Urdu",
}

# Spoken filler said to the caller while the history DB lookup runs — must be
# in the agent's locked language so an English agent never speaks Urdu.
HISTORY_FILLERS: dict[str, str] = {
    "en":  "One moment, let me check the records.",
    "ur":  "ایک منٹ، میں ریکارڈ check کرتا ہوں۔",
}

# Maps detected language code → Whisper language code for STT.
LANGUAGE_WHISPER_MAP: dict[str, str] = {
    "en":  "en",
    "ur":  "ur",
}


# Whisper outputs these phrases when it hears silence or background noise.
_WHISPER_HALLUCINATIONS: frozenset[str] = frozenset({
    "thank you", "thanks", "thank you for watching", "thanks for watching",
    "good ideas are born", "you needle deer", "please subscribe",
    "like and subscribe", "see you next time", "don't forget to subscribe",
    "hmm", "um", "uh", "oh", "ah",
})


class STTNoiseFilter(FrameProcessor):
    """Drops TranscriptionFrames that are Whisper noise or hallucinations.

    Three checks (any match → frame is silently dropped):
    1. Text shorter than MIN_CHARS — single-char presses, mic pops, etc.
       Meaningful short replies (ji/han/ok/no…) and any digit are exempt so a
       caller answering "ji" or "ok" is never silently dropped.
    2. Known Whisper hallucination phrases — phrases Whisper emits on silence.
    3. Bengali / Devanagari dominant text — Whisper misreads Pakistani audio
       as these scripts; they never occur in real Pakistani caller speech.
    """

    # 2, not 3 — at 3 the common one/two-letter affirmatives ("ji", "ok",
    # "no", "ha", "جی", "ہا") were dropped, so the caller's reply vanished and
    # the bot appeared not to hear them.
    MIN_CHARS = 2

    # Meaningful one-character replies that must survive the length check.
    _SHORT_ALLOW: frozenset[str] = frozenset({
        "ji", "g", "ha", "hn", "ok", "no", "na",
        "جی", "ہا", "گ", "نہ", "ہاں",
    })

    def __init__(self, on_transcription: Optional[Callable[[], None]] = None, **kwargs):
        super().__init__(**kwargs)
        # Optional hook fired with every TranscriptionFrame that survives the
        # noise checks below — used by end_call_handler to detect the caller
        # still speaking during the post-end_call grace window.
        self._on_transcription = on_transcription

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = (frame.text or "").strip()

            normalized = text.lower().rstrip(" .!?,،۔")
            # Keep short text when it is a real reply or carries a digit
            # (e.g. a single-digit answer to "how many?").
            meaningful_short = normalized in self._SHORT_ALLOW or any(c.isdigit() for c in text)
            if len(text) < self.MIN_CHARS and not meaningful_short:
                logger.debug(f"STTNoiseFilter: dropped short transcription: {text!r}")
                return

            if normalized in _WHISPER_HALLUCINATIONS:
                logger.debug(f"STTNoiseFilter: dropped hallucination: {text!r}")
                return

            non_ascii = [c for c in text if ord(c) >= 128]
            if non_ascii:
                bd_count = sum(1 for c in non_ascii if 0x0900 <= ord(c) <= 0x09FF)
                if bd_count / len(non_ascii) > 0.4:
                    logger.debug(f"STTNoiseFilter: dropped Bengali/Devanagari: {text!r}")
                    return

            if self._on_transcription is not None:
                self._on_transcription()

        await self.push_frame(frame, direction)




# ---------------------------------------------------------------------------
# end_call tool — lets the LLM terminate the call gracefully
# ---------------------------------------------------------------------------

def _build_end_call_tools(lang_name: str) -> ToolsSchema:
    """Tool descriptions in the agent's own reply language — a purely-English
    agent previously got Urdu-only tool descriptions regardless of default_language."""
    english = lang_name == "English"

    end_call_desc = (
        "End the call. Use this when: (1) the caller says goodbye, bye, or any "
        "farewell word. (2) the caller has no more questions and wants to end the call."
        if english else
        "کال ختم کریں۔ استعمال کریں جب: "
        "(1) صارف خدا حافظ، اللہ حافظ، bye، goodbye یا کوئی الوداعی لفظ کہے۔ "
        "(2) صارف کا کوئی سوال نہ ہو اور وہ کال ختم کرنا چاہے۔"
    )
    history_desc = (
        "Search the database for the caller's previous call records (extracted data). "
        "Call this when the caller asks about any of their previous conversations — e.g. "
        "'What did I tell you before?', 'Was my appointment/booking/order/complaint recorded "
        "before?', 'What's my previous record?', or asks about a specific phone number's "
        "record. If the caller gives a phone number, pass it in phone_number, otherwise "
        "leave it empty (the caller's own number will be used). Before calling this "
        "function, tell the caller a short line like 'One moment, let me check.'"
        if english else
        "کالر کی پچھلی calls کا محفوظ شدہ ریکارڈ (extracted data) ڈیٹابیس میں تلاش کریں۔ "
        "جب کالر اپنی کسی بھی پچھلی بات چیت کے بارے میں پوچھے — مثلاً 'میں نے پہلے کیا بتایا تھا؟'، "
        "'کیا میری appointment/booking/order/شکایت پہلے درج ہوئی تھی؟'، "
        "'میرا پچھلا ریکارڈ کیا ہے؟'، یا کسی فون نمبر کا ریکارڈ پوچھے — تو یہ function call کریں۔ "
        "اگر کالر کوئی فون نمبر بتائے تو وہ phone_number میں بھیجیں، ورنہ خالی چھوڑ دیں "
        "(خود کالر کا نمبر استعمال ہوگا)۔ "
        "function call سے پہلے کالر کو ایک مختصر جملہ کہیں کہ 'ایک منٹ، میں check کرتا ہوں'۔"
    )
    phone_desc = (
        "Optional — the phone number whose record to look up (e.g. 03244283400 or "
        "+923244283400). Leave empty if the caller doesn't provide a number."
        if english else
        "اختیاری — وہ فون نمبر جس کا ریکارڈ دیکھنا ہے "
        "(مثلاً 03244283400 یا +923244283400)۔ "
        "اگر کالر نمبر نہ بتائے تو یہ خالی چھوڑ دیں۔"
    )

    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="end_call",
                description=end_call_desc,
                properties={},
                required=[],
            ),
            FunctionSchema(
                name="check_caller_history",
                description=history_desc,
                properties={
                    "phone_number": {
                        "type": "string",
                        "description": phone_desc,
                    },
                },
                required=[],
            ),
        ]
    )

# ---------------------------------------------------------------------------
# Per-agent RAG cache — async-safe with per-agent locks
# ---------------------------------------------------------------------------

# Unbounded growth guard: as more users/agents are added over the platform's
# lifetime this dict would otherwise never shrink. Simple FIFO eviction (dicts
# preserve insertion order) — the oldest entry is dropped once the cap is hit;
# a dropped agent just rebuilds its RAG on its next call.
_RAG_CACHE_MAX_SIZE = 200
_rag_cache: dict[str, ScriptRAG] = {}
_rag_locks: dict[str, asyncio.Lock] = {}


async def _get_agent_rag(agent: dict, user_id: str = "") -> ScriptRAG | None:
    """Build and cache the RAG for an agent's DB script. Cache key is user_id:agent_id."""
    agent_id = agent.get("id", "")
    if not agent_id:
        return None

    cache_key = f"{user_id}:{agent_id}" if user_id else agent_id

    if cache_key in _rag_cache:
        return _rag_cache[cache_key]

    _rag_locks.setdefault(cache_key, asyncio.Lock())
    async with _rag_locks[cache_key]:
        if cache_key in _rag_cache:
            return _rag_cache[cache_key]

        script_data = agent.get("scripts") or {}
        content = script_data.get("content", "")
        if not content:
            logger.warning(f"Agent '{agent.get('name')}' has no script content — RAG disabled.")
            return None

        logger.info(f"Building RAG for agent '{agent.get('name')}' from DB script…")
        rag = await ScriptRAG.from_content(content, openai_api_key=os.getenv("OPENAI_API_KEY", ""))
        if len(_rag_cache) >= _RAG_CACHE_MAX_SIZE:
            oldest_key = next(iter(_rag_cache))
            _rag_cache.pop(oldest_key, None)
            _rag_locks.pop(oldest_key, None)
        _rag_cache[cache_key] = rag
        logger.info(f"Agent RAG ready — {rag.chunk_count} chunks.")
        return rag


# ---------------------------------------------------------------------------
# Caller history context builder
# ---------------------------------------------------------------------------

def _build_caller_history_context(caller_history: list, lang: str = "ur") -> str:
    """Convert caller history list into a context string for the LLM.

    Phrased in the agent's locked language (English when lang == "en", Urdu
    otherwise) so an English agent is never fed Urdu instructions. Always
    returns a non-empty string so the agent knows it has access to caller
    history — even when the list is empty (first-time caller).
    """
    english = lang == "en"

    def _fmt_date(started: str) -> str:
        if not started:
            return ""
        try:
            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return str(started)[:10]

    if english:
        lines = ["=== This caller's previous call records ==="]
        if not caller_history:
            lines.append("No previous call was found in the records for this number.")
        else:
            for i, call in enumerate(caller_history, 1):
                started = _fmt_date(call.get("started_at", ""))
                ed = call.get("extracted_data") or {}
                if ed:
                    fields = ", ".join(f"{k}: {v}" for k, v in ed.items() if v)
                    lines.append(f"Call {i} ({started}): {fields}")
                else:
                    lines.append(f"Call {i} ({started}): no details available")
        lines.append(
            "Instruction: If the caller asks about any of their previous conversations, "
            "information, or records — answer from the records above. "
            "State only what is in the records; if something is not there, clearly say it "
            "was not found in the records. "
            "For more detail or a specific phone number, use check_caller_history. "
            "Do not re-ask for things the caller has already provided."
        )
        return "\n".join(lines)

    lines = ["=== اس کالر کی پچھلی کالوں کا ریکارڈ ==="]
    if not caller_history:
        lines.append("اس نمبر کی کوئی پچھلی call ریکارڈ میں نہیں ملی۔")
    else:
        for i, call in enumerate(caller_history, 1):
            started = _fmt_date(call.get("started_at", ""))
            ed = call.get("extracted_data") or {}
            if ed:
                fields = "، ".join(f"{k}: {v}" for k, v in ed.items() if v)
                lines.append(f"کال {i} ({started}): {fields}")
            else:
                lines.append(f"کال {i} ({started}): تفصیل دستیاب نہیں")

    lines.append(
        "ہدایت: اگر کالر اپنی کسی بھی پچھلی بات چیت، معلومات، یا ریکارڈ کے بارے میں پوچھے — "
        "تو اوپر دیے گئے ریکارڈ سے جواب دیں۔ "
        "جو معلومات ریکارڈ میں ہو وہی بتائیں، جو نہ ہو اسے صاف کہیں کہ ریکارڈ میں نہیں ملی۔ "
        "زیادہ تفصیل یا کسی مخصوص نمبر کے لیے check_caller_history استعمال کریں۔ "
        "جو چیزیں پہلے بتائی جا چکی ہیں انہیں دوبارہ نہ پوچھیں۔"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bot pipeline
# ---------------------------------------------------------------------------

async def run_bot(
    transport: BaseTransport,
    runner_args: RunnerArguments,
    hangup_callback=None,
    is_outbound: bool = False,
    agent: dict | None = None,
    db_call_id: str | None = None,
    call_control_id: str | None = None,
    caller_history: list | None = None,
    caller_phone: str | None = None,
    user_id: str = "",
):
    logger.info(f"Starting bot — agent={agent.get('name') if agent else 'none'}")

    # RAG: build in background with user-scoped cache key
    rag_task = asyncio.create_task(_get_agent_rag(agent, user_id)) if agent else None

    # Extraction target fields from the agent's script — drives the generic,
    # domain-agnostic "what to collect" reminder injected each turn.
    script_cfg = (agent.get("scripts") or {}) if agent else {}
    extraction_fields = script_cfg.get("extraction_fields") or []

    # Voice map and system prompt — always from agent config
    default_lang = (agent.get("default_language") or "ur") if agent else "ur"

    if agent:
        voice_map: dict[str, str] = {
            "en":  agent.get("voice_english", VOICE_ENGLISH),
            "ur":  agent.get("voice_urdu",    get_default_urdu_voice()),
        }
        system_prompt = agent.get("system_prompt_override") or _FALLBACK_PROMPT
    else:
        voice_map = {
            "en":  VOICE_ENGLISH,
            "ur":  get_default_urdu_voice(),
        }
        system_prompt = _FALLBACK_PROMPT

    initial_voice = voice_map.get(default_lang, voice_map["ur"])

    # Mutable holder so the end_call handler can cancel the task after it's created
    task_holder: list = [None]

    async with aiohttp.ClientSession() as session:
        # Default STT language = Urdu so Whisper transcribes Pakistani callers correctly
        # from the very first turn (no cold-start auto-detect delay).
        stt = GroqSTTService(
            api_key=os.getenv("GROQ_API_KEY"),
            language=LANGUAGE_WHISPER_MAP.get(default_lang, "ur"),
        )

        # TTS engine is chosen once from the agent's locked language:
        #   en → ElevenLabs        ur → UpliftAI Orator (streaming)
        # The call is single-language for its whole duration, so there is no
        # mid-call engine switch.
        if default_lang == "en":
            # agents.voice_english may still hold a legacy Uplift "v_..." id from
            # before the ElevenLabs switch — ignore those and use the system voice.
            english_voice = (agent.get("voice_english") if agent else "") or ""
            if not english_voice or english_voice.startswith("v_"):
                english_voice = ELEVENLABS_VOICE_ID
            tts = ElevenLabsTTSService(
                api_key=ELEVENLABS_API_KEY,
                settings=ElevenLabsTTSService.Settings(
                    voice=english_voice,
                    model=ELEVENLABS_MODEL,
                ),
            )
            logger.info(f"TTS engine: ElevenLabs (voice={english_voice}, model={ELEVENLABS_MODEL})")
        else:
            # Streaming Urdu TTS (Orator). voice_id is the agent's chosen Urdu voice.
            tts = UpliftStreamingTTSService(
                api_key=os.getenv("UPLIFT_API_KEY"),
                voice_id=initial_voice,
                aiohttp_session=session,
            )
            logger.info(f"TTS engine: UpliftAI Orator (voice={initial_voice})")

        # Groq is primary (fast TTFB); OpenAI is a hot-standby. If Groq errors
        # mid-call (rate limit, capacity, timeout), ServiceSwitcherStrategyFailover
        # swaps to OpenAI for the rest of the call instead of the call dying.
        groq_llm = GroqLLMService(
            api_key=os.getenv("GROQ_API_KEY"),
            model="llama-3.3-70b-versatile",
        )
        openai_llm_fallback = OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="gpt-4o",
        )
        llm = ServiceSwitcher(
            services=[groq_llm, openai_llm_fallback],
            strategy_type=ServiceSwitcherStrategyFailover,
        )

        # Set whenever the caller says anything meaningful — end_call_handler
        # watches this to catch the LLM asking a question and calling end_call
        # in the same turn (a Llama/Groq habit) without waiting for the reply.
        caller_spoke_event = asyncio.Event()
        noise_filter = STTNoiseFilter(on_transcription=caller_spoke_event.set)
        # No runtime language/voice switcher: the call is locked to one language
        # and one TTS engine (chosen above). The lock is enforced in the prompt +
        # STT layer (LANGUAGE RULE system message + STT language seed).

        # end_call handler: wait up to 5s (long enough for the farewell TTS to
        # finish) before actually hanging up. If the caller speaks again during
        # that window — e.g. answering a confirmation question the LLM asked in
        # the same breath as calling end_call — treat the call as still active
        # and skip the hangup instead of cutting them off mid-reply.
        async def end_call_handler(params: FunctionCallParams):
            logger.info("end_call tool triggered — waiting to confirm the caller has nothing more to say")
            await params.result_callback({"status": "ending"})
            caller_spoke_event.clear()
            try:
                await asyncio.wait_for(caller_spoke_event.wait(), timeout=5)
                logger.info("Caller spoke during end_call grace period — call continues, not hanging up")
                return
            except asyncio.TimeoutError:
                pass
            if hangup_callback is not None:
                await hangup_callback()
            if task_holder[0] is not None:
                await task_holder[0].cancel()

        async def check_caller_history_handler(params: FunctionCallParams):
            from app.core.database import search_caller_records
            from app.core.phone import normalize_phone

            # Speak an immediate filler so the caller isn't met with silence
            # while the DB lookup runs (modern pipecat pattern: filler in handler).
            await params.llm.push_frame(TTSSpeakFrame(HISTORY_FILLERS.get(default_lang, HISTORY_FILLERS["ur"])))

            # Prefer a number the caller spoke; otherwise use their own line.
            spoken = (params.arguments or {}).get("phone_number")
            phone = normalize_phone(spoken) or normalize_phone(caller_phone)
            agent_id = agent.get("id") if agent else None

            if not phone:
                await params.result_callback(
                    {"found": False, "message": "فون نمبر معلوم نہیں — کالر سے نمبر پوچھیں۔"}
                )
                return

            records = await search_caller_records(phone, agent_id=agent_id)
            logger.info(f"check_caller_history: phone={phone} records={len(records)}")

            if not records:
                await params.result_callback({
                    "found": False,
                    "phone": phone,
                    "message": "اس نمبر کا کوئی پچھلا ریکارڈ نہیں ملا۔",
                })
                return

            await params.result_callback({
                "found": True,
                "phone": phone,
                "record_count": len(records),
                "records": records,
            })

        for _llm_service in (groq_llm, openai_llm_fallback):
            _llm_service.register_function("end_call", end_call_handler)
            _llm_service.register_function("check_caller_history", check_caller_history_handler)

        messages = [{"role": "system", "content": system_prompt}]
        # Language lock — the agent's default_language is authoritative. The bot
        # must reply ONLY in this language, even if the caller uses another one or
        # the reference script is written in a different language.
        lang_name = LANGUAGE_NAMES.get(default_lang, "Urdu")
        messages.append({"role": "system", "content": (
            f"LANGUAGE RULE — You MUST speak and reply ONLY in {lang_name} for the entire call. "
            f"Always answer in {lang_name}, even if the caller speaks a different language and even "
            f"if the reference script or any other instruction is written in another language. "
            f"Never switch languages. This rule overrides everything else except the NUMBER RULE."
        )})
        history_ctx = _build_caller_history_context(caller_history or [], default_lang)
        messages.append({"role": "system", "content": history_ctx})
        logger.info(f"Caller history injected: {len(caller_history or [])} previous call(s)")
        messages.append({"role": "system", "content": (
            "NUMBER RULE — strictly follow this every time you speak a number:\n"
            "1. Phone numbers: say every digit in English words — "
            "e.g. 03244284000 → 'zero three two four four two eight four zero zero zero'.\n"
            "2. Amounts/fees/prices/order totals/bills/quantities: say in English words — "
            "e.g. 1500 → 'fifteen hundred', 5000 → 'five thousand', 500 → 'five hundred', "
            "250 rupees → 'two hundred fifty rupees'.\n"
            "3. Dates/times: say in English — "
            "e.g. 'tomorrow', 'six pm', 'Wednesday', 'next Friday'.\n"
            "4. Any other number: say in English digits or words, never in Urdu.\n"
            "5. This applies even when the reference script writes the number or price in Urdu "
            "words (e.g. 'پندرہ سو روپے' or 'ڈھائی سو') — you MUST convert it and say the English "
            "equivalent ('fifteen hundred rupees', 'two hundred fifty'). NEVER speak a number, "
            "total, or price in Urdu words.\n"
            "This rule overrides everything else."
        )})
        messages.append({"role": "system", "content": (
            "CONVERSATION FLOW RULE — strictly follow this every turn:\n"
            "1. Ask about ONE thing at a time, then stop and wait for the caller's answer. "
            "Never ask a question and then answer it yourself in the same turn.\n"
            "2. Never say a confirmation question (e.g. \"Is that correct?\") and then immediately "
            "proceed as if the caller already said yes. Wait for their next reply before confirming "
            "or closing anything.\n"
            "3. Do not repeat the same transition phrase more than once per call (e.g. \"I just need "
            "to confirm a few more details\"). Vary your wording or drop the filler entirely.\n"
            "4. Only call end_call after the caller has actually confirmed the order/request in their "
            "own turn — never in the same turn where you first ask for confirmation."
        )})
        end_call_tools = _build_end_call_tools(lang_name)
        context = LLMContext(messages, tools=end_call_tools)
        # Faster turn-taking: the default stop strategy runs the semantic Smart Turn
        # model, which adds 1-4s of "has the caller finished?" latency per turn (and
        # loads a model per call). Replace it with a pure VAD-timeout stop so the bot
        # responds as soon as the caller pauses.
        user_turn_params = LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
            ),
        )
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context, user_params=user_turn_params
        )

        rtvi = RTVIProcessor()
        call_id = uuid.uuid4().hex[:8]
        convo_logger = ConversationLogger(
            call_id=call_id,
            db_call_id=db_call_id,
            call_control_id=call_control_id,
        )
        metrics_collector = CallMetricsCollector(db_call_id=db_call_id)

        # Decide whether to wire RAG synchronously from the script content — do NOT
        # await the RAG build here, or the opening greeting is delayed by the script
        # embedding time (~5-8s on a cold cache). The injector resolves rag_task
        # lazily on the first caller turn, by which point the build is usually done.
        has_script = bool((script_cfg.get("content") or "").strip())

        # Build pipeline — RAGContextInjector only added when agent has a script
        pipeline_stages = [
            transport.input(),
            rtvi,
            stt,
            noise_filter,
            user_aggregator,
        ]
        if has_script and rag_task is not None:
            pipeline_stages.append(RAGContextInjector(
                rag_task=rag_task, top_k=3, extraction_fields=extraction_fields,
                response_language=lang_name,
            ))
        pipeline_stages.extend([
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ])

        pipeline = Pipeline(pipeline_stages)

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[
                RTVIObserver(rtvi),
                convo_logger,
                metrics_collector,
                DebugLogObserver(
                    frame_types={
                        TTSTextFrame: (BaseOutputTransport, FrameEndpoint.SOURCE),
                    }
                ),
            ],
            idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
        )
        task_holder[0] = task

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected (outbound={is_outbound})")
            if is_outbound:
                # Outbound: direct TTS greeting — no LLM delay.
                agent_name = (agent.get("name") or "") if agent else ""
                _OUT_GREETINGS = {
                    "ur":  f"السلام علیکم! {agent_name} کی طرف سے آپ کو کال کی جا رہی ہے۔ کیا آپ کے پاس چند لمحے ہیں؟",
                    "en":  f"Hello! This is {agent_name} calling. Do you have a moment to talk?",
                }
                out_greeting = _OUT_GREETINGS.get(default_lang, _OUT_GREETINGS["ur"])
                messages.append({"role": "assistant", "content": out_greeting})
                await task.queue_frames([TTSSpeakFrame(out_greeting)])
            else:
                # Inbound: play the greeting directly — skips LLM TTFT entirely.
                # Prefer pre-synthesized cached audio (no cold TTS TTFB); fall back
                # to live TTS if the cache miss/synth fails.
                g_engine, g_voice, g_key, g_model, greeting_text = resolve_inbound_greeting(agent)
                # Add to context as assistant message so LLM doesn't re-greet
                messages.append({"role": "assistant", "content": greeting_text})

                cached = await get_greeting_pcm(
                    g_engine, g_voice, greeting_text,
                    api_key=g_key, session=session, model=g_model,
                )
                if cached:
                    pcm, rate = cached
                    # ~100 ms chunks (even byte count for 16-bit samples).
                    chunk = max(2, (rate * 2) // 10)
                    if chunk % 2:
                        chunk += 1
                    frames: list = [TTSStartedFrame()]
                    for i in range(0, len(pcm), chunk):
                        frames.append(TTSAudioRawFrame(pcm[i:i + chunk], rate, 1))
                    frames.append(TTSStoppedFrame())
                    await task.queue_frames(frames)
                    logger.info(f"Greeting played from cache ({g_engine}, {len(pcm)} bytes PCM)")
                else:
                    await task.queue_frames([TTSSpeakFrame(greeting_text)])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected")
            if hangup_callback is not None:
                await hangup_callback()
            await task.cancel()

        runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
        try:
            await runner.run(task)
        finally:
            # Guarantee the final turns are persisted before this call's pipeline
            # tears down — post-call extraction reads them from the DB.
            await convo_logger.flush()
            await metrics_collector.flush()


async def bot(
    runner_args: RunnerArguments,
    hangup_callback=None,
    is_outbound: bool = False,
    agent: dict | None = None,
    db_call_id: str | None = None,
    call_control_id: str | None = None,
    caller_history: list | None = None,
    caller_phone: str | None = None,
    user_id: str = "",
):
    """Main bot entry point compatible with Pipecat Cloud."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(
        transport,
        runner_args,
        hangup_callback=hangup_callback,
        is_outbound=is_outbound,
        agent=agent,
        db_call_id=db_call_id,
        call_control_id=call_control_id,
        caller_history=caller_history,
        caller_phone=caller_phone,
        user_id=user_id,
    )


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
