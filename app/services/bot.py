#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.utils import create_stream_resampler
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMRunFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStoppedSpeakingFrame,
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
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies, UserTurnStrategies
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cerebras.llm import CerebrasLLMService
from pipecat.services.together.llm import TogetherLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.realtime.events import (
    AudioInput,
    AudioOutput,
    AudioConfiguration,
    InputAudioTranscription,
    PCMAudioFormat,
    SessionProperties,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService, OpenAIRealtimeLLMSettings
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from app.services.browser_ws_serializer import _BrowserEventBridge
from app.services.tts import UpliftStreamingTTSService
from app.services.call_metrics_collector import CallMetricsCollector
from app.services.conversation_logger import ConversationLogger
from app.services.greeting_cache import get_greeting_pcm
from app.services.rag import RAGContextInjector, ScriptRAG, build_conv_state_message
from app.core.llm_config import get_llm_config, DEFAULT_MODEL as DEFAULT_LLM_MODEL
from app.core.pipeline_config import REALTIME_PROVIDERS, get_pipeline_config
from app.core.stt_config import get_stt_config
from app.core.tts_config import get_tts_config

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Transport configs
# ---------------------------------------------------------------------------

# 1.0s silence before confirming the caller stopped talking. Was 0.6s — measured
# with the real Silero analyzer that a caller reciting a CNIC/phone/account
# number in two breath groups (e.g. "42101" <pause> "1234567" <pause> "1")
# leaves a ~0.9s gap between groups, which 0.6s confirmed as end-of-turn: the
# number got split into two disconnected turns, so the LLM only ever saw the
# first half. 1.0s tolerates that natural pause while still being well under
# a full second-guessing silence for normal short replies.
_VAD_STOP_SECS = 1.0

# Hard failsafe for LLMUserAggregatorParams.user_turn_stop_timeout — see the
# comment at its use site (run_bot's turn-strategy construction) for the
# live-observed bug this bounds. Pipecat's own default is 5.0s.
_USER_TURN_STOP_FAILSAFE_SECS = 2.0

# One-way-audio watchdog (see _AudioFrameProbe / on_client_connected in
# run_bot): how long to wait after a real Telnyx call connects before
# concluding that literally zero caller audio ever arrived — a carrier-side
# media-path issue observed intermittently on real outbound calls, where
# Telnyx confirms streaming.started and the WS handshake looks completely
# normal but no InputAudioRawFrame ever reaches the pipeline. In a healthy
# call the first audio frame (background/room noise, not speech — this
# doesn't wait for the caller to say anything) arrives within milliseconds
# of on_client_connected, confirmed live: audio_probe.count was already 1
# in the same log timestamp as "Client connected". This isn't waiting for
# the caller to talk, just for the media path to prove it's carrying
# *anything* — so it can be short and still have a wide safety margin over
# the 30-60s it took callers to give up and hang up on their own.
_AUDIO_WATCHDOG_DELAY_SECS = 6.0
_NO_AUDIO_APOLOGY = {
    "ur": "معذرت، لگتا ہے لائن میں آواز کا مسئلہ ہے۔ ہم آپ سے تھوڑی دیر بعد دوبارہ رابطہ کریں گے۔ اللہ حافظ۔",
    "en": "Sorry, it looks like there's an audio issue on this line — we'll try reaching you again shortly. Goodbye.",
}

# EXPERIMENT (pipecat-upgrade branch only) — see the _USE_SMART_TURN branch
# in run_bot()'s turn-strategy construction for the full trade-off. Reverted
# to False after live testing turned up something worse than the documented
# latency cost: a Test Agent session where a passing (non-hallucination,
# non-empty) transcription — "بھولیں" — got a VAD "user started speaking"
# and Smart Turn logged "EndOfTurnState.COMPLETE", but the turn's own
# "stopped speaking" event only fired ~5s later with strategy=None, and no
# RAG/LLM/TTS activity ever followed for that turn or the rest of the
# session — a real turn silently never reaching the LLM, not just a slow
# one. That's strictly worse than the 1-4s latency trade-off this was
# testing, so reverting to the proven SpeechTimeoutUserTurnStopStrategy
# path until the Smart Turn integration is investigated further.
_USE_SMART_TURN = False

# Spoken when the LLM (primary or fallback) reports an error mid-call —
# confirmed live: a transient Together AI 503 left a turn with no reply at
# all (ServiceSwitcherStrategyFailover only fails over on errors that mark a
# service permanently unusable, not this kind of hiccup — see run_bot()'s
# on_error handlers), and the caller sat in silence for 22s before hanging
# up. This doesn't fix the provider outage, just stops the caller being met
# with dead air when one happens.
# How many times one user turn may be handed to a different LLM before the bot
# stops trying and apologises instead. Failover wraps around the service list,
# so without a cap two providers that are both unhappy would pass the same turn
# back and forth for as long as the caller waited.
_LLM_FAILOVER_MAX_RETRIES = 2

_LLM_ERROR_RECOVERY = {
    "ur": "معذرت، ایک لمحے کے لیے تکنیکی مسئلہ ہوا۔ براہِ کرم اپنی بات دوبارہ کہیں۔",
    "en": "Sorry, I had a brief technical hiccup. Could you please repeat that?",
}

# Silent-caller handling. Observed live (2026-09-09): the bot asked for a phone
# number, the caller then said nothing at all for 25s (confirmed against the
# call recording — the line sat at -70dB, true silence, so VAD was right to
# stay quiet), and the bot just waited. Nothing re-engaged the caller, so the
# turn only ended when they gave up and said goodbye. pipecat has the hook for
# this (LLMUserAggregatorParams.user_idle_timeout → on_user_turn_idle) but it
# defaults to 0 = disabled.
#
# The idle timer re-arms on every BotStoppedSpeakingFrame, so each nudge below
# naturally schedules the next one — hence the explicit nudge cap, after which
# the call is closed politely rather than nudging forever at someone who has
# walked away.
# 12s, not less: this agent asks for CNIC and account numbers, and the LONG
# NUMBER CAPTURE RULE explicitly invites the caller to pause mid-number — a
# caller reading a card off a table should not be interrupted. Two nudges then
# a close puts the hang-up at ~36s of genuine silence.
_USER_IDLE_TIMEOUT_SECS = 12.0
_IDLE_MAX_NUDGES = 2
_IDLE_NUDGES = {
    "ur": [
        "کیا آپ لائن پر ہیں؟",
        "معذرت، مجھے آپ کی آواز نہیں آ رہی۔ کیا آپ دوبارہ کہہ سکتے ہیں؟",
    ],
    "en": [
        "Are you still there?",
        "Sorry, I can't hear you. Could you say that again?",
    ],
}
_IDLE_GIVE_UP = {
    "ur": "لگتا ہے آپ مصروف ہیں۔ ہم آپ سے بعد میں رابطہ کر لیں گے۔ اللہ حافظ۔",
    "en": "It seems you're busy right now — we'll reach out again later. Goodbye.",
}

# pipecat 1.0+: VAD is configured via LLMUserAggregatorParams.vad_analyzer,
# not on transport params (removed field — see run_bot()'s turn-strategy
# construction). This dict is pipecat's own dev-CLI fallback, unused by the
# real webhook-driven call path (bot()/run_bot() are invoked directly).
transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
    "telnyx": lambda: FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
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
# TTS config
# ---------------------------------------------------------------------------

# ElevenLabs powers every language's TTS (see the engine note in run_bot).
# API key + default voice stay system-level (platform-paid); the model id is
# a per-user choice from Settings → Voice Engine (app/core/tts_config.py).
# Voice is chosen once per call from the agent's locked default_language.
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")

# UpliftAI — opt-in alternative TTS engine (Settings → Voice Engine). See the
# TTS engine note in run_bot for why it isn't the default.
UPLIFT_API_KEY  = os.getenv("UPLIFT_API_KEY", "")
UPLIFT_VOICE_ID = os.getenv("UPLIFT_VOICE_ID", "v_8eelc901")

# Static inbound greeting per language — pre-synthesizable (see greeting_cache).
INBOUND_GREETINGS: dict[str, str] = {
    "ur":  "السلام علیکم! آپ کا شکریہ کال کرنے کا۔ میں آپ کی کیا مدد کر سکتا ہوں؟",
    "en":  "Hello! Thank you for calling. How can I help you today?",
}


async def resolve_inbound_greeting(agent: dict | None) -> tuple[str, str, str, str | None, str, float]:
    """Return (engine, voice_id, api_key, model, text, speed) for an agent's
    inbound greeting. Engine follows the owning user's Settings → Voice
    Engine selection (ElevenLabs or UpliftAI — see the TTS engine note in
    run_bot); model is that same selection's model id (None for UpliftAI,
    which has no model variants); speed is the resolved speaking-rate
    override (1.0 = normal). Used by both the live call and startup prewarm
    so the two never drift.

    text is the agent's own greeting_text (Settings → Voice & Language) when
    set, falling back to the platform default for its language — every agent
    used to get the same hardcoded greeting regardless of what it does.
    """
    default_lang = (agent.get("default_language") or "ur") if agent else "ur"
    custom_text = ((agent.get("greeting_text") if agent else "") or "").strip()
    text = custom_text or INBOUND_GREETINGS.get(default_lang, INBOUND_GREETINGS["ur"])
    voice_field = "voice_english" if default_lang == "en" else "voice_urdu"
    agent_voice = ((agent.get(voice_field) if agent else "") or "").strip()

    provider, model, speed = await get_tts_config((agent.get("user_id") if agent else "") or "", agent=agent)
    if provider == "uplift":
        voice = agent_voice if agent_voice.startswith("v_") else UPLIFT_VOICE_ID
        return "uplift", voice, UPLIFT_API_KEY, None, text, speed

    # ElevenLabs: legacy rows stored an Uplift "v_..." id here — fall back to
    # the system voice.
    voice = agent_voice if agent_voice and not agent_voice.startswith("v_") else ELEVENLABS_VOICE_ID
    return "elevenlabs", voice, ELEVENLABS_API_KEY, model, text, speed


async def prewarm_agent_greeting(agent: dict, session) -> bool:
    """Pre-synthesize and cache an agent's inbound greeting. Returns True on success."""
    engine, voice, api_key, model, text, speed = await resolve_inbound_greeting(agent)
    res = await get_greeting_pcm(engine, voice, text, api_key=api_key, session=session, model=model, speed=speed)
    return res is not None


_greeting_prewarm_tasks: set[asyncio.Task] = set()


def prewarm_agent_greeting_background(agent: dict) -> None:
    """Kick off a greeting re-synthesis right after an edit changes greeting_text
    (or the language/voice it's spoken in), instead of leaving it fully lazy —
    the cache is content-addressed by text (see greeting_cache.py), so a new
    greeting_text is a guaranteed cache miss and the caller's first turn would
    otherwise pay the live-TTS fallback latency once."""
    async def _run():
        async with aiohttp.ClientSession() as session:
            await prewarm_agent_greeting(agent, session)

    task = asyncio.create_task(_run())
    _greeting_prewarm_tasks.add(task)
    task.add_done_callback(_greeting_prewarm_tasks.discard)


def _install_telnyx_wire_probe() -> None:
    """Log what Telnyx actually puts on the WebSocket, before deserialization.

    _AudioFrameProbe below sits AFTER the serializer, so a zero count there
    is ambiguous: either Telnyx sent no media at all (carrier / tunnel), or it
    sent media that TelnyxFrameSerializer.deserialize() dropped — it returns
    None for an unrecognized event and, silently, for any media payload that
    decodes to zero bytes. Those two causes need completely different fixes,
    so count the raw messages here to tell them apart.

    Wraps the class method (the serializer is constructed inside pipecat's
    create_transport, so there's no instance to wrap at our call site). Always
    delegates; a failure here must never take down a live call.
    """
    from pipecat.serializers.telnyx import TelnyxFrameSerializer

    if getattr(TelnyxFrameSerializer, "_wire_probe_installed", False):
        return
    original = TelnyxFrameSerializer.deserialize

    async def deserialize(self, data):
        frame = await original(self, data)
        try:
            self._wire_total = getattr(self, "_wire_total", 0) + 1
            if frame is None:
                self._wire_dropped = getattr(self, "_wire_dropped", 0) + 1
            if self._wire_total in (1, 10, 100) or self._wire_total % 500 == 0:
                kind = "?"
                if isinstance(data, (str, bytes)):
                    try:
                        kind = json.loads(data).get("event", "?")
                    except Exception:
                        kind = "non-json"
                logger.info(
                    f"[telnyx-wire] raw messages from Telnyx: {self._wire_total} "
                    f"(dropped by deserializer: {getattr(self, '_wire_dropped', 0)}, "
                    f"latest event={kind})"
                )
        except Exception:
            pass
        return frame

    TelnyxFrameSerializer.deserialize = deserialize
    TelnyxFrameSerializer._wire_probe_installed = True


_install_telnyx_wire_probe()


def _install_realtime_unknown_event_tolerance() -> None:
    """Survive realtime events pipecat's OpenAI parser doesn't know.

    grok_voice reuses OpenAIRealtimeLLMService because xAI documents the same
    protocol shape — but "same shape" is not "same event set". xAI sends a
    `ping` keepalive, which is not among the 31 types
    events._server_event_types knows, so parse_server_event raised inside
    _receive_task_handler and killed the receive task outright: after the very
    first ping nothing from Grok reached the pipeline again and the caller sat
    in silence. Confirmed live (2026-09-10 18:54:31): "Unimplemented server
    event type: ping", followed by no further audio for the rest of the call.

    Unknown events are skipped generally rather than `ping` specifically —
    nothing promises ping is the only place the two protocols diverge, and one
    unrecognised event must not cost the whole call. This is deliberately
    narrow: an event type pipecat DOES know that fails to validate still
    raises, because that is a real bug rather than a protocol difference. The
    dispatch chain in _receive_task_handler ends without an else, so an event
    matching no branch is already ignored safely; this only stops the parse
    from raising before it gets there.
    """
    from pipecat.services.openai.realtime import events as _events

    if getattr(_events, "_unknown_event_tolerance_installed", False):
        return
    original = _events.parse_server_event

    class _UnknownServerEvent:
        """Carries a type that matches no dispatch branch, so it is skipped."""

        def __init__(self, event_type):
            self.type = event_type

    def parse_server_event(raw):
        try:
            return original(raw)
        except Exception:
            try:
                event_type = json.loads(raw).get("type")
            except Exception:
                raise  # Not an unknown type — the payload itself is unparseable.
            if event_type in _events._server_event_types:
                raise  # Known type that failed validation: a real bug, not a dialect gap.
            logger.debug(f"[realtime] ignoring unknown server event: {event_type}")
            return _UnknownServerEvent(event_type)

    _events.parse_server_event = parse_server_event
    _events._unknown_event_tolerance_installed = True


_install_realtime_unknown_event_tolerance()


class _AudioFrameProbe(FrameProcessor):
    """Confirms whether raw caller audio is even reaching the pipeline (vs.
    VAD/STT silently never triggering on audio that did arrive). Logs the
    first frame and then every 100th.

    Also backs the one-way-audio watchdog in run_bot()/on_client_connected:
    a handful of real outbound Telnyx calls have been observed with the
    media WS reporting success (streaming.started, correct encoding parsed)
    but literally zero InputAudioRawFrames arriving for the call's entire
    remaining duration — a carrier-side RTP/media-path issue this app has no
    way to repair, but .count lets the watchdog at least notice it and end
    the call quickly instead of leaving the caller in dead air."""

    def __init__(self):
        super().__init__()
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._count += 1
            if self._count == 1 or self._count % 100 == 0:
                logger.info(f"[audio-probe] input audio frames received so far: {self._count}")
        await self.push_frame(frame, direction)


class _RealtimeVADGate(FrameProcessor):
    """Silero-VAD-driven turn detection for OpenAI Realtime mode, used in
    place of OpenAI's own server-side turn_detection.

    Root-caused via a direct Telnyx-protocol simulation (bypassing the need
    for a real phone call): audio demonstrably reaches OpenAIRealtimeLLMService
    correctly over the Telnyx path (confirmed byte-for-byte against the
    serializer's output), and OpenAI's server does detect speech STARTING
    (interruption fires reliably) — but never auto-creates a reply, i.e. it
    never reliably detects the caller has STOPPED speaking over this
    resampled-to-24kHz audio path, even though the same service/pipeline code
    worked correctly in the browser-widget test (raw 24kHz passthrough, no
    resampling). Silero VAD can't run at 24kHz directly (only 8000/16000), so
    this processor keeps its own resampled 16kHz copy purely for VAD analysis
    and drives turn-taking explicitly instead of trusting OpenAI's own
    detection — the same proven mechanism (Silero) already used everywhere
    else in this system, same stop_secs too (see _VAD_STOP_SECS above).
    """

    def __init__(self):
        super().__init__()
        self._analyzer = SileroVADAnalyzer(sample_rate=16000, params=VADParams(stop_secs=_VAD_STOP_SECS))
        # Normally a transport calls this during StartFrame handling — used
        # standalone here, so it must be called explicitly, or num_frames_required()
        # silently reads sample_rate=0 (never set) and every analyze_audio() call
        # raises AttributeError on the internal buffer-size fields it also sets.
        self._analyzer.set_sample_rate(16000)
        self._resampler = create_stream_resampler()
        self._buffer = b""
        self._frame_bytes = self._analyzer.num_frames_required() * 2  # 16-bit samples
        self._speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            resampled = await self._resampler.resample(frame.audio, frame.sample_rate, 16000)
            self._buffer += resampled
            while len(self._buffer) >= self._frame_bytes:
                chunk, self._buffer = self._buffer[: self._frame_bytes], self._buffer[self._frame_bytes :]
                state = await self._analyzer.analyze_audio(chunk)
                if state == VADState.SPEAKING and not self._speaking:
                    self._speaking = True
                    await self.broadcast_interruption()
                elif state == VADState.QUIET and self._speaking:
                    self._speaking = False
                    await self.broadcast_frame(UserStoppedSpeakingFrame)
        await self.push_frame(frame, direction)


class _RealtimeOutputSmoother(FrameProcessor):
    """Jitter buffer for OpenAI Realtime's output audio.

    OpenAIRealtimeLLMService pushes a TTSAudioRawFrame the instant each
    response.audio.delta arrives over its own WebSocket to OpenAI — nothing
    smooths OpenAI's own network delivery jitter before it reaches the
    telephony transport, whose real-time-paced send queue just runs dry
    (audible gap on the live call) the moment OpenAI's delivery has any delay.
    Reported by the user as "awaz cut rahi hai" right after Realtime mode
    started actually replying.

    Fix: hold back the first ~250ms of each response's frames (in original
    order) before releasing them in one burst. The transport's own send queue
    then carries that ~250ms as a standing cushion for the rest of the
    response, which absorbs normal delivery jitter without an audible gap —
    reusing the transport's existing real-time pacing rather than
    reimplementing a separate timed drain loop.

    Only TTS response-content frames are buffered — everything else (control
    frames, EndFrame/CancelFrame, etc.) passes straight through so pipeline
    shutdown and other machinery can't get stuck behind this. InterruptionFrame
    drops whatever's buffered immediately, since stale audio must never play
    after the caller barges in.
    """

    BUFFER_MS = 250

    _RESPONSE_CONTENT_TYPES = (
        TTSStartedFrame, TTSAudioRawFrame, TTSTextFrame, LLMFullResponseEndFrame, TTSStoppedFrame,
    )

    def __init__(self):
        super().__init__()
        self._pending: list[Frame] = []
        self._buffered_audio_ms = 0.0
        self._buffering = True

    async def _flush(self, direction: FrameDirection):
        pending, self._pending = self._pending, []
        self._buffered_audio_ms = 0.0
        for f in pending:
            await self.push_frame(f, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        is_response_content = (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, self._RESPONSE_CONTENT_TYPES)
        )

        if not is_response_content:
            if isinstance(frame, InterruptionFrame):
                self._pending = []
                self._buffered_audio_ms = 0.0
                self._buffering = True
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TTSStartedFrame):
            self._pending = []
            self._buffered_audio_ms = 0.0
            self._buffering = True

        if self._buffering:
            self._pending.append(frame)
            if isinstance(frame, TTSAudioRawFrame):
                self._buffered_audio_ms += (len(frame.audio) / 2 / frame.sample_rate) * 1000
            if self._buffered_audio_ms >= self.BUFFER_MS or isinstance(
                frame, (TTSStoppedFrame, LLMFullResponseEndFrame)
            ):
                self._buffering = False
                await self._flush(direction)
        else:
            await self.push_frame(frame, direction)
            if isinstance(frame, TTSStoppedFrame):
                self._buffering = True


# Human-readable language names — used to instruct the LLM which language to
# reply in. The agent's `default_language` is authoritative: the bot speaks
# ONLY this language for the whole call, regardless of what the caller uses.
LANGUAGE_NAMES: dict[str, str] = {
    "en":  "English",
    "ur":  "Urdu",
}

# Spoken filler said to the caller while the history DB lookup runs — must be
# in the agent's locked language so an English agent never speaks Urdu. The
# Urdu form is passive/gender-neutral: agents may have a female persona (e.g.
# "عائشہ") and the old "میں ... کرتا ہوں" was masculine.
HISTORY_FILLERS: dict[str, str] = {
    "en":  "One moment, let me check the records.",
    "ur":  "ایک منٹ، ریکارڈ چیک کیا جا رہا ہے۔",
}

# Maps detected language code → Whisper language code for STT.
LANGUAGE_WHISPER_MAP: dict[str, str] = {
    "en":  "en",
    "ur":  "ur",
}


# Whisper outputs these phrases when it hears silence or background noise.
# Confirmed live: "موسیقی" (music) fired twice on near-silent audio mid-call
# (2026-09-08 18:50:23/25), right in the middle of CNIC digit collection —
# this list was English-only, so it slipped through as a real user turn and
# fed the LLM a nonsense "answer" at exactly the point it needed to track
# accumulated digits across turns. Whisper's multilingual models are known to
# hallucinate "[Music]"/"music" (and its translation) on silence/background
# noise regardless of the transcription language.
#
# "شکریہ" ("thank you") was added here alongside "موسیقی" on the assumption
# that Whisper's Urdu hallucinations mirror its English ones ("thanks for
# watching") — that was wrong and caused a real regression: a caller
# genuinely saying "شکریہ" is an extremely common, completely normal thing
# to say (confirmed live: the bot's own greeting elsewhere in this file uses
# it), and this silently dropped that turn instead of responding to it.
# Removed, along with "میوزک"/"سبسکرائب کریں" which were the same
# unverified-translation guess, never actually observed. Only add an entry
# here again after directly observing it as a hallucination, the way
# "موسیقی" was confirmed — not by translating an English hallucination.
_WHISPER_HALLUCINATIONS: frozenset[str] = frozenset({
    "thank you", "thanks", "thank you for watching", "thanks for watching",
    "good ideas are born", "you needle deer", "please subscribe",
    "like and subscribe", "see you next time", "don't forget to subscribe",
    "hmm", "um", "uh", "oh", "ah",
    "موسیقی",
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


# Number-word markers used by _looks_like_number_fragment() to recognize a
# transcription that's mostly a spoken digit sequence — both native Urdu
# digit words and the phonetic Urdu-script transliterations Whisper produces
# when a caller says English digit words. Confirmed live: a caller's CNIC
# arrived as "سیون ڈبل نائن تری فور ٹو زیو تری" for "seven double nine three
# four two zero three" — "زیو" is Whisper's own (inconsistent) spelling of
# "zero" next to the more common "زیرو", both included since either can show
# up depending on the utterance.
_NUMBER_WORD_MARKERS: frozenset[str] = frozenset({
    # Native Urdu digit words
    "صفر", "زیرو", "ایک", "دو", "تین", "چار", "پانچ", "چھ", "سات", "آٹھ", "نو",
    # English digit words transliterated into Urdu script
    "زیو", "ون", "ٹو", "تھری", "تری", "فور", "فائیو", "سکس", "سیون", "ایٹ", "نائن",
    "ڈبل",
})


def _looks_like_number_fragment(text: str) -> bool:
    """Heuristic: is this transcription mostly a spoken digit sequence (raw
    digits and/or digit words) rather than a normal conversational reply?
    Used by LongNumberAccumulator to stitch a long number (CNIC, phone,
    account number) back together when the caller pauses partway through
    and it lands as a separate turn."""
    text = text.strip()
    if not text:
        return False
    words = text.split()
    if len(words) > 8:
        return False
    if any(c.isdigit() for c in text):
        return True
    marker_count = sum(1 for w in words if w.strip(" .!?,،۔") in _NUMBER_WORD_MARKERS)
    return marker_count > 0 and marker_count / len(words) >= 0.5


class LongNumberAccumulator(FrameProcessor):
    """Stitches a long number (CNIC, phone, account number) back together
    when the caller pauses partway through it and it lands as separate
    turns — confirmed live: a caller's CNIC came in as "4236" then, in a
    separate turn, "سیون ڈبل نائن تری فور ٹو زیو تری" ("seven double nine
    three four two zero three"). The LLM (Together-hosted openai/gpt-oss-120b
    in the call this was root-caused from) never reliably carried the first
    fragment forward across the "please continue" turns the LONG NUMBER
    CAPTURE RULE system message asks it to send — it kept correctly asking
    the caller to continue, but the earlier digits were effectively lost
    from its working context, and after a few rounds it abandoned the field
    entirely and jumped to an unrelated question.

    This doesn't parse the number itself — it rewrites each new fragment's
    TranscriptionFrame.text to include everything accumulated so far, so the
    LLM only ever reasons about ONE current, complete-so-far utterance
    instead of reconstructing one from turns spread across its own repeated
    prompts. The actual "is this complete / read it back / confirm" judgment
    stays with the LLM (via the LONG NUMBER CAPTURE RULE) — the part it
    already handled correctly once it isn't also responsible for remembering
    the earlier fragments itself.
    """

    # Safety valve — if accumulation runs this long, something's off (e.g. a
    # run of short unrelated replies that happen to look numeric); stop
    # growing and let the next fragment start a fresh buffer instead of
    # feeding the LLM an ever-growing wall of stitched text.
    MAX_BUFFER_CHARS = 150

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._buffer: str = ""
        self._active: bool = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = (frame.text or "").strip()
            if _looks_like_number_fragment(text):
                if self._active and len(self._buffer) < self.MAX_BUFFER_CHARS:
                    self._buffer = f"{self._buffer} {text}".strip()
                    frame.text = self._buffer
                    logger.debug(f"LongNumberAccumulator: stitched fragment, now: {self._buffer!r}")
                else:
                    self._buffer = text
                    self._active = True
            elif self._active:
                logger.debug("LongNumberAccumulator: episode ended (non-fragment turn)")
                self._buffer = ""
                self._active = False

        await self.push_frame(frame, direction)




# ---------------------------------------------------------------------------
# end_call tool — lets the LLM terminate the call gracefully
# ---------------------------------------------------------------------------

def _build_end_call_tools(lang_name: str, include_search_tool: bool = False) -> ToolsSchema:
    """Tool descriptions in the agent's own reply language — a purely-English
    agent previously got Urdu-only tool descriptions regardless of default_language.

    include_search_tool adds search_knowledge_base — only used in Realtime
    mode, where RAG retrieval has to be a callable tool instead of the
    RAGContextInjector pipeline stage cascaded mode uses (see run_bot)."""
    english = lang_name == "English"

    # The farewell words are named explicitly because the LLM otherwise kept
    # the caller on a call they had clearly ended. The wrap-up sentence is the
    # counterweight, added after a live call where the caller went quiet
    # mid-question and then said "اللہ حافظ" — the bot replied with a bare
    # "اللہ حافظ، آپ کا شکریہ!" and hung up, dropping the summary its own
    # system prompt asks for. Never gate the hang-up itself on that summary:
    # a caller who wants to go must always be let go, on the same turn.
    end_call_desc = (
        "End the call. Use this when: (1) the caller says goodbye, bye, or any "
        "farewell word. (2) the caller has no more questions and wants to end the call. "
        "In the SAME reply that ends the call, first give one short closing line — "
        "what you noted (e.g. their name and what they were interested in) and that a "
        "representative will follow up — then say goodbye. Keep it to one sentence, and "
        "never refuse or delay ending the call just because some details are missing."
        if english else
        "کال ختم کریں۔ استعمال کریں جب: "
        "(1) صارف خدا حافظ، اللہ حافظ، bye، goodbye یا کوئی الوداعی لفظ کہے۔ "
        "(2) صارف کا کوئی سوال نہ ہو اور وہ کال ختم کرنا چاہے۔ "
        "جس جواب میں کال ختم کر رہے ہوں، اُسی میں پہلے ایک مختصر اختتامی جملہ کہیں — "
        "جو معلومات نوٹ ہوئیں (مثلاً نام اور دلچسپی) اور یہ کہ نمائندہ رابطہ کرے گا — "
        "پھر الوداع کہیں۔ ایک جملے سے زیادہ نہ ہو، اور کچھ معلومات ادھوری ہونے کی وجہ سے "
        "کال ختم کرنے سے ہرگز انکار یا تاخیر نہ کریں۔"
    )
    history_desc = (
        "Search the database for the caller's previous call records (extracted data). "
        "Call this when the caller asks about any of their previous conversations — e.g. "
        "'What did I tell you before?', 'Was my appointment/booking/order/complaint recorded "
        "before?', 'What's my previous record?', or asks about a specific phone number's "
        "record. If the caller gives a phone number, pass it in phone_number, otherwise "
        "leave it empty (the caller's own number will be used). Before calling this "
        "function, tell the caller a short line like 'One moment, let me check.' "
        "Do NOT use this for prices, fees, calculations, addresses, office locations, or "
        "anything answered by the reference script — it ONLY searches this caller's own "
        "past call records. Do NOT call this on a simple greeting ('hello', 'hi', 'assalam o "
        "alaikum') or at the very start of the call — only call it once the caller has "
        "actually asked about a past interaction or record."
        if english else
        "کالر کی پچھلی calls کا محفوظ شدہ ریکارڈ (extracted data) ڈیٹابیس میں تلاش کریں۔ "
        "جب کالر اپنی کسی بھی پچھلی بات چیت کے بارے میں پوچھے — مثلاً 'میں نے پہلے کیا بتایا تھا؟'، "
        "'کیا میری appointment/booking/order/شکایت پہلے درج ہوئی تھی؟'، "
        "'میرا پچھلا ریکارڈ کیا ہے؟'، یا کسی فون نمبر کا ریکارڈ پوچھے — تو یہ function call کریں۔ "
        "اگر کالر کوئی فون نمبر بتائے تو وہ phone_number میں بھیجیں، ورنہ خالی چھوڑ دیں "
        "(خود کالر کا نمبر استعمال ہوگا)۔ "
        "function call سے پہلے کالر کو ایک مختصر جملہ کہیں کہ 'ایک منٹ، ریکارڈ چیک کیا جا رہا ہے'۔ "
        "قیمت، فیس، حساب کتاب، پتہ، دفتر کی لوکیشن، یا reference script میں موجود کسی بھی معلومات "
        "کے لیے یہ function ہرگز استعمال نہ کریں — یہ صرف اسی کالر کی پچھلی calls کا ریکارڈ "
        "تلاش کرتا ہے۔ صرف سلام دعا ('ہیلو'، 'السلام علیکم') پر یا کال کے بالکل شروع میں یہ "
        "function ہرگز نہ بلائیں — صرف تب بلائیں جب کالر واقعی اپنی پچھلی بات چیت یا ریکارڈ کے "
        "بارے میں پوچھے۔"
    )
    phone_desc = (
        "Optional — the phone number whose record to look up (e.g. 03244283400 or "
        "+923244283400). Leave empty if the caller doesn't provide a number."
        if english else
        "اختیاری — وہ فون نمبر جس کا ریکارڈ دیکھنا ہے "
        "(مثلاً 03244283400 یا +923244283400)۔ "
        "اگر کالر نمبر نہ بتائے تو یہ خالی چھوڑ دیں۔"
    )

    tools = [
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
    if include_search_tool:
        search_desc = (
            "Search the agent's reference script/knowledge base for specific "
            "details you're unsure about — prices, fees, procedures, policies, "
            "addresses. Use this whenever the caller asks something concrete "
            "and you don't already know the exact answer from your instructions. "
            "Do NOT use this for a simple greeting or small talk ('hello', 'hi', "
            "'how are you') — just reply naturally and wait for an actual question."
            if english else
            "ایجنٹ کے reference script/knowledge base میں مخصوص تفصیلات تلاش کریں "
            "جن کے بارے میں آپ کو یقین نہیں — قیمتیں، فیس، طریقہ کار، پالیسیاں، پتے۔ "
            "جب کالر کوئی مخصوص سوال پوچھے اور آپ کو اپنی instructions سے صحیح جواب "
            "معلوم نہ ہو تو یہ function استعمال کریں۔ "
            "صرف سلام دعا یا عام بات چیت ('ہیلو'، 'السلام علیکم'، 'کیسے ہیں') پر یہ function "
            "استعمال نہ کریں — فطری انداز میں جواب دیں اور اصل سوال کا انتظار کریں۔"
        )
        tools.append(FunctionSchema(
            name="search_knowledge_base",
            description=search_desc,
            properties={
                "query": {
                    "type": "string",
                    "description": "A short search query describing what information you need.",
                },
            },
            required=["query"],
        ))
    return ToolsSchema(standard_tools=tools)

# ---------------------------------------------------------------------------
# Per-agent RAG cache — async-safe with per-agent locks
# ---------------------------------------------------------------------------

# Unbounded growth guard: as more users/agents are added over the platform's
# lifetime this dict would otherwise never shrink. LRU eviction: every cache
# hit re-inserts the key at the end (dicts preserve insertion order), so the
# entry dropped at the cap is the least recently *used* agent, not merely the
# oldest-created one. A dropped agent just rebuilds its RAG on its next call.
#
# NOTE: this cache — and the PATCH-time invalidation + prewarm in
# app/api/agents.py / app/api/scripts.py — is per-process. If uvicorn ever
# runs with workers>1, a script edit only reaches the worker that served the
# PATCH; the others keep answering from the stale RAG. Move invalidation to
# Redis pub/sub before scaling workers.
_RAG_CACHE_MAX_SIZE = 200
_rag_cache: dict[str, ScriptRAG] = {}
_rag_locks: dict[str, asyncio.Lock] = {}


def _rag_cache_get(cache_key: str) -> ScriptRAG | None:
    """Cache lookup with LRU touch — hit re-inserts the key at the end."""
    rag = _rag_cache.pop(cache_key, None)
    if rag is not None:
        _rag_cache[cache_key] = rag
    return rag


async def _get_agent_rag(agent: dict, user_id: str = "") -> ScriptRAG | None:
    """Build and cache the RAG for an agent's DB script. Cache key is user_id:agent_id."""
    agent_id = agent.get("id", "")
    if not agent_id:
        return None

    cache_key = f"{user_id}:{agent_id}" if user_id else agent_id

    rag = _rag_cache_get(cache_key)
    if rag is not None:
        return rag

    _rag_locks.setdefault(cache_key, asyncio.Lock())
    async with _rag_locks[cache_key]:
        rag = _rag_cache_get(cache_key)
        if rag is not None:
            return rag

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


# In-flight background prewarm tasks. We keep strong references because a bare
# asyncio.create_task() can be garbage-collected mid-execution (same risk noted
# in conversation_logger.py), which would silently drop the rebuild.
_rag_prewarm_tasks: set[asyncio.Task] = set()


def prewarm_agent_rag_background(agent: dict, user_id: str = "") -> None:
    """Kick off a RAG rebuild right after a script/agent edit invalidates the
    cache, instead of leaving it fully lazy. Without this, the cache pop alone
    means the rebuild only starts on the next incoming call, and the caller's
    first turn blocks on RAGContextInjector awaiting the ~5-8s embedding build —
    dead air right after the edit that prompted it.
    """
    task = asyncio.create_task(_get_agent_rag(agent, user_id))
    _rag_prewarm_tasks.add(task)
    task.add_done_callback(_rag_prewarm_tasks.discard)


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


def build_static_system_messages(
    system_prompt: str, default_lang: str, caller_history: list | None = None,
) -> tuple[list[dict], str]:
    """Assemble the fixed system-message stack every call sends: the agent's
    own prompt, LANGUAGE RULE, caller-history context, NUMBER RULE, and
    CONVERSATION FLOW RULE. Returns (messages, lang_name).

    Shared by run_bot (live calls) and the dashboard's agent-test widget
    (app/api/agent_test.py) so a test session sees byte-identical instructions
    to what a real caller's agent would — no separate copy to drift out of
    sync.
    """
    lang_name = LANGUAGE_NAMES.get(default_lang, "Urdu")
    messages = [{"role": "system", "content": system_prompt}]
    # Language lock — the agent's default_language is authoritative. The bot
    # must reply ONLY in this language, even if the caller uses another one or
    # the reference script is written in a different language.
    #
    # Scoping note: each platform rule below states the narrow scope it
    # governs instead of claiming blanket supremacy. Three stacked messages
    # each saying "overrides everything else" taught the LLM to deprioritize
    # the agent's own system prompt entirely — the user-authored prompt above
    # must stay the authority on role, personality, and conversation content.
    messages.append({"role": "system", "content": (
        f"LANGUAGE RULE — You MUST speak and reply ONLY in {lang_name} for the entire call. "
        f"Always answer in {lang_name}, even if the caller speaks a different language and even "
        f"if the reference script or any other instruction is written in another language. "
        f"Never switch languages. This rule governs ONLY which language you speak — your role, "
        f"personality, and what you actually say always come from your main instructions above."
    )})
    history_ctx = _build_caller_history_context(caller_history or [], default_lang)
    messages.append({"role": "system", "content": history_ctx})
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
        "This rule overrides everything else.\n"
        "Scope: this rule governs how YOU pronounce numbers. NEVER ask the caller to say "
        "numbers in English or correct how the caller speaks — accept their numbers in any "
        "language or format."
    )})
    messages.append({"role": "system", "content": (
        "LONG NUMBER CAPTURE RULE — for any long number you ask the caller for "
        "(CNIC, phone number, account number, card/reference number — anything "
        "8+ digits):\n"
        "1. When you first ask for it, tell the caller they can say it in a couple of short "
        "groups with a brief pause if that's easier — you don't need to instruct them to say "
        "it all in one breath.\n"
        "2. If what the caller just said looks like an INCOMPLETE number (clearly fewer digits "
        "than a real CNIC/phone/account number should have, e.g. only 5 digits of a 13-digit "
        "CNIC), do NOT treat it as the final answer and do NOT move on. Ask them to continue "
        "('please continue with the rest of the digits') instead of re-asking for the whole "
        "number from scratch.\n"
        "3. If the caller's very next turn is ALSO just digits (no other new topic), treat it as "
        "a CONTINUATION of the same number and append it to what they already gave you — this "
        "is very likely one number that got split across two turns because of a brief pause, "
        "not two separate pieces of information.\n"
        "4. Once you have what looks like the complete number, read it back to the caller digit "
        "by digit and ask them to confirm it's correct before using it or moving on. If they "
        "correct any digit, use their correction.\n"
        "5. NEVER ask the caller to say a number again once they have already given it — you "
        "already have it in this conversation, so re-asking makes it look like you forgot. If "
        "their confirmation is unclear, garbled, or never arrives, read back the digits you "
        "already have ONE more time and ask a plain yes/no ('is this correct?'). If it is still "
        "unclear after that, accept the number you have, say you'll have a representative verify "
        "it, and move on to the next topic — do not restart the number from scratch and do not "
        "keep asking about it.\n"
        "6. This does not change the NUMBER RULE above — you still SPEAK the read-back in "
        "English digit words; this rule is only about correctly COLLECTING what the caller says."
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
        "own turn — never in the same turn where you first ask for confirmation.\n"
        "5. NEVER say goodbye or call end_call while the caller is still waiting for an answer, "
        "calculation, or information you said you would provide. Deliver the answer first — "
        "saying 'one moment, let me calculate' and then ending the call is a critical failure.\n"
        "These are default guards for natural phone turn-taking. If your main instructions "
        "explicitly define a different flow for a specific step, follow your main instructions "
        "for that step."
    )})
    return messages, lang_name


# ---------------------------------------------------------------------------
# Primary LLM builder — provider/model selected in Settings → AI Model
# ---------------------------------------------------------------------------

def _model_extra_params(model: str) -> dict:
    """Per-model request params that keep reasoning models usable for voice.

    - gpt-oss (Groq & Cerebras): reasoning_effort MUST stay "low" — the default
      ("medium") thinks for 10-25s before the first content token, measured as
      exactly that much dead air per turn on real calls.
    - qwen3 on Groq: reasoning_format "hidden" strips <think> blocks from the
      content stream — without it the TTS would literally SPEAK the model's
      chain of thought to the caller.
    """
    if "gpt-oss" in model:
        return {"reasoning_effort": "low"}
    if "qwen" in model:
        return {"reasoning_format": "hidden"}
    return {}


def _build_primary_llm(provider: str, model: str, temperature: float | None = None):
    """Build the primary call LLM from the global llm_config selection.

    Never breaks a live call: an unusable selection (e.g. cerebras/together
    chosen but its API key isn't in .env) falls back to the Groq default model.

    temperature=None omits the field entirely (pipecat's Settings.temperature
    defaults to NOT_GIVEN, so the provider's own model default applies) —
    only sent when an agent/account explicitly overrides it.
    """
    if provider == "cerebras":
        api_key = os.getenv("CEREBRAS_API_KEY", "")
        if api_key:
            logger.info(f"Primary LLM: Cerebras / {model} (temperature={temperature})")
            return CerebrasLLMService(
                api_key=api_key,
                settings=CerebrasLLMService.Settings(
                    model=model,
                    temperature=temperature,
                    extra=_model_extra_params(model),
                ),
            )
        logger.warning(
            "LLM config selects Cerebras but CEREBRAS_API_KEY is not set — "
            f"falling back to Groq / {DEFAULT_LLM_MODEL}"
        )
        provider, model = "groq", DEFAULT_LLM_MODEL

    if provider == "together":
        api_key = os.getenv("TOGETHER_API_KEY", "")
        if api_key:
            logger.info(f"Primary LLM: Together AI / {model} (temperature={temperature})")
            return TogetherLLMService(
                api_key=api_key,
                settings=TogetherLLMService.Settings(
                    model=model,
                    temperature=temperature,
                    extra=_model_extra_params(model),
                ),
            )
        logger.warning(
            "LLM config selects Together AI but TOGETHER_API_KEY is not set — "
            f"falling back to Groq / {DEFAULT_LLM_MODEL}"
        )
        provider, model = "groq", DEFAULT_LLM_MODEL

    logger.info(f"Primary LLM: Groq / {model} (temperature={temperature})")
    return GroqLLMService(
        api_key=os.getenv("GROQ_API_KEY"),
        settings=GroqLLMService.Settings(
            model=model,
            temperature=temperature,
            extra=_model_extra_params(model),
        ),
    )


def _build_stt(provider: str, model: str, language_code: str):
    """Build the call's STT service from the global stt_config selection.

    Never breaks a live call: an unusable selection (deepgram/together chosen
    but its API key isn't in .env) falls back to Groq.

    Groq and Deepgram each use one fixed model — not a per-call knob:
    - Groq: whisper-large-v3 (not -turbo — turbo's Urdu word-error rate
      produced transcripts like "tag spoiler" for "tax filer"; see the STT
      construction comment this replaces for the full history).
    - Deepgram: nova-3-general — the only Deepgram tier with Urdu support
      (nova-2 rejects language=ur outright). Continuous websocket streaming:
      every audio frame is sent as it arrives and Deepgram's own server does
      endpointing, instead of GroqSTTService's local-VAD-gated batching
      (SegmentedSTTService — only calls out to Whisper once Silero VAD marks
      an utterance boundary). interim_results is off so Deepgram only ever
      emits finalized TranscriptionFrames, same as Whisper — one frame shape
      flowing through the rest of the pipeline regardless of provider.

    Together AI's model IS a per-call knob (its account hosts several
    transcription models — see app/core/stt_config.py) — it's OpenAISTTService
    pointed at Together's OpenAI-compatible /v1/audio/transcriptions endpoint,
    the same Whisper-API shape GroqSTTService itself uses under the hood.
    """
    if provider == "deepgram":
        api_key = os.getenv("DEEPGRAM_API_KEY", "")
        if api_key:
            logger.info(f"Primary STT: Deepgram (nova-3-general, lang={language_code})")
            return DeepgramSTTService(
                api_key=api_key,
                settings=DeepgramSTTService.Settings(
                    language=language_code,
                    interim_results=False,
                ),
            )
        logger.warning(
            "STT config selects Deepgram but DEEPGRAM_API_KEY is not set — "
            "falling back to Groq / whisper-large-v3"
        )

    if provider == "together":
        api_key = os.getenv("TOGETHER_API_KEY", "")
        if api_key:
            logger.info(f"Primary STT: Together AI ({model}, lang={language_code})")
            return OpenAISTTService(
                api_key=api_key,
                base_url="https://api.together.xyz/v1",
                settings=OpenAISTTService.Settings(
                    model=model,
                    language=language_code,
                ),
            )
        logger.warning(
            "STT config selects Together AI but TOGETHER_API_KEY is not set — "
            "falling back to Groq / whisper-large-v3"
        )

    logger.info(f"Primary STT: Groq (whisper-large-v3, lang={language_code})")
    return GroqSTTService(
        api_key=os.getenv("GROQ_API_KEY"),
        settings=GroqSTTService.Settings(
            model="whisper-large-v3",
            language=language_code,
        ),
    )


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
    caller_history_task: asyncio.Task | None = None,
    caller_phone: str | None = None,
    user_id: str = "",
    browser_event_dedup: dict | None = None,
):
    """browser_event_dedup: pass the BrowserFrameSerializer instance's .dedup
    dict to enable _BrowserEventBridge (see that class's docstring) — only
    the agent-test widget's WS route needs this; live Telnyx calls (and the
    dedup-free TelnyxFrameSerializer they use) leave this None."""
    logger.info(f"Starting bot — agent={agent.get('name') if agent else 'none'}")

    # RAG: build in background with user-scoped cache key
    rag_task = asyncio.create_task(_get_agent_rag(agent, user_id)) if agent else None

    async def _warm_rag_connection():
        # Runs concurrently with greeting playback / the caller's first turn
        # of silence — by the time the caller actually finishes speaking,
        # the RAG client's OpenAI connection is already warm (see
        # ScriptRAG.warm_connection's docstring for why this matters).
        try:
            rag = await rag_task
            if rag is not None:
                await rag.warm_connection()
        except Exception:
            pass

    if rag_task is not None:
        asyncio.create_task(_warm_rag_connection())

    # Extraction target fields from the agent's script — drives the generic,
    # domain-agnostic "what to collect" reminder injected each turn.
    script_cfg = (agent.get("scripts") or {}) if agent else {}
    extraction_fields = script_cfg.get("extraction_fields") or []
    has_script = bool((script_cfg.get("content") or "").strip())

    # System prompt — always from agent config
    default_lang = (agent.get("default_language") or "ur") if agent else "ur"
    system_prompt = (agent.get("system_prompt_override") if agent else None) or _FALLBACK_PROMPT

    # Voice pipeline mode comes from this user's Settings → Voice Pipeline
    # Mode selection (per-user, read fresh per call, same pattern as the
    # LLM/STT/TTS selections below). Falls back to cascaded if the selected
    # provider's platform API key isn't set, even if the user picked it.
    pipeline_mode, realtime_voice = await get_pipeline_config(user_id, agent=agent)
    realtime_provider = REALTIME_PROVIDERS.get(pipeline_mode)
    is_realtime = realtime_provider is not None and bool(
        os.getenv(realtime_provider["api_key_env"])
    )

    # Mutable holder so the end_call handler can cancel the task after it's created
    task_holder: list = [None]

    async with aiohttp.ClientSession() as session:
        if is_realtime:
            # No separate STT/TTS stages — OpenAIRealtimeLLMService handles
            # audio in/out directly (see the pipeline construction below).
            stt = None
            tts = None
            noise_filter = None
            stt_provider = None
            logger.info(f"Voice pipeline: {realtime_provider['label']} (voice={realtime_voice}, lang={default_lang})")
        else:
            # STT provider comes from this user's Settings → STT Engine selection
            # (per-user, read fresh per call — same pattern as the LLM selection
            # below). Default language = Urdu so callers are transcribed correctly
            # from the very first turn (no cold-start auto-detect delay).
            stt_provider, stt_model, stt_endpointing_ms = await get_stt_config(user_id, agent=agent)
            stt = _build_stt(stt_provider, stt_model, LANGUAGE_WHISPER_MAP.get(default_lang, "ur"))

            # TTS engine: this user's Settings → Voice Engine selection
            # (per-user, read fresh per call — same pattern as the LLM/STT
            # selections above). ElevenLabs is the default for every language;
            # eleven_turbo_v2_5 doesn't officially market Urdu support, but a
            # TTS→STT round-trip test (synthesize Urdu, transcribe it back
            # with Whisper) came back a near-exact match to the source text.
            #
            # UpliftAI Orator is available as an explicit opt-in: it ran Urdu
            # by default until its streaming endpoint proved unreliable in
            # production (observed a ~19s socket stall mid-call that made the
            # caller hang up before a retry could recover). UpliftStreamingTTSService
            # (app/services/tts.py) now fails fast on a stalled connection
            # (sock_read=5s, total=12s) and retries once before any audio has
            # reached the caller — a real improvement over the original
            # incident, but still unverified against a live production call
            # volume, so it stays opt-in per user rather than the default.
            #
            # agents.voice_english / voice_urdu may still hold a legacy Uplift
            # "v_..." id from before the ElevenLabs switch — for ElevenLabs,
            # ignore those and use the system default voice; for UpliftAI,
            # that same id is exactly what's needed.
            tts_provider, tts_model, tts_speed = await get_tts_config(user_id, agent=agent)
            voice_field = "voice_english" if default_lang == "en" else "voice_urdu"
            agent_voice = ((agent.get(voice_field) if agent else "") or "").strip()
            if tts_provider == "uplift":
                resolved_voice = agent_voice if agent_voice.startswith("v_") else UPLIFT_VOICE_ID
                tts = UpliftStreamingTTSService(
                    api_key=UPLIFT_API_KEY,
                    voice_id=resolved_voice,
                    speed=tts_speed,
                    aiohttp_session=session,
                    sample_rate=22050,
                )
                logger.info(f"TTS engine: UpliftAI (voice={resolved_voice}, speed={tts_speed}, lang={default_lang})")
            else:
                resolved_voice = agent_voice if agent_voice and not agent_voice.startswith("v_") else ELEVENLABS_VOICE_ID
                tts = ElevenLabsTTSService(
                    api_key=ELEVENLABS_API_KEY,
                    settings=ElevenLabsTTSService.Settings(
                        voice=resolved_voice,
                        model=tts_model,
                        speed=tts_speed,
                    ),
                )
                logger.info(f"TTS engine: ElevenLabs (voice={resolved_voice}, model={tts_model}, speed={tts_speed}, lang={default_lang})")

        # Primary LLM comes from this user's Settings → AI Model selection
        # (per-user, read fresh per call). OpenAI is a hot-standby: if the
        # primary errors mid-call (rate limit, capacity, timeout),
        # ServiceSwitcherStrategyFailover swaps to it for the rest of the call
        # instead of the call dying. Not used in Realtime mode — there is no
        # second speech-to-speech provider configured to fail over to.
        #
        # IMPORTANT — confirmed live (Together AI 503 mid-call): pipecat's
        # ServiceSwitcherStrategyFailover only switches once the errored
        # service's own is_usable flips False, and FrameProcessor.push_error
        # flips that only for a permanent-category error. A transient "service
        # unavailable" is precisely what that strategy is documented to leave
        # alone ("errors the service can carry on from"), so no switch ever
        # happened — and with nothing else listening, that turn's LLM call
        # simply vanished: no reply, no retry, caller left in silence until
        # they gave up and hung up.
        #
        # _FailoverOnAnyLLMError closes that gap. A transient error is still
        # worth moving away from when there is somewhere to move to, so it
        # switches on any error from the active service once the parent has
        # declined to. But switching alone does NOT rescue the turn that
        # failed — it only routes the next one — so on_service_switched
        # re-runs the same context on the newly active service. That re-run is
        # what actually turns dead air into an answer.
        #
        # The split between the two handlers is load-bearing, not stylistic:
        # push_error fires on_error BEFORE the ErrorFrame reaches the switcher,
        # so at on_error time the failed service is still the active one and a
        # retry queued there would go straight back to it. Hence
        # on_service_switched retries, and on_error only apologises — and only
        # when there is no failover left to try.
        if not is_realtime:
            llm_provider, llm_model, llm_temperature = await get_llm_config(user_id, agent=agent)
            primary_llm = _build_primary_llm(llm_provider, llm_model, llm_temperature)
            openai_llm_fallback = OpenAILLMService(
                api_key=os.getenv("OPENAI_API_KEY"),
                settings=OpenAILLMService.Settings(model="gpt-4o"),
            )
            llm_services = [primary_llm, openai_llm_fallback]
            # Reset per user turn (see _reset_idle_nudges) so a long call gets a
            # fresh budget each turn rather than spending it once and
            # apologising for the rest of the call.
            llm_failover_retries = [0]

            class _FailoverOnAnyLLMError(ServiceSwitcherStrategyFailover):
                """Fail over on errors the service could have carried on from.

                The parent handles the permanent case and returns None for
                everything else. Anything it declines is a transient error on
                the active service, which is worth switching away from here:
                the caller has no reply either way, and the other provider is
                sitting idle.
                """

                async def handle_error(self, error):
                    switched = await super().handle_error(error)
                    if switched is not None:
                        return switched
                    failed = error.processor or self.active_service
                    if failed is not self.active_service:
                        return None
                    if llm_failover_retries[0] >= _LLM_FAILOVER_MAX_RETRIES:
                        logger.warning(
                            "LLM failover budget spent for this turn — not switching again"
                        )
                        return None
                    current_idx = self.services.index(self.active_service)
                    for offset in range(1, len(self.services)):
                        candidate = self.services[(current_idx + offset) % len(self.services)]
                        if candidate.is_usable:
                            llm_failover_retries[0] += 1
                            return await self._set_active_if_available(candidate)
                    return None

            llm = ServiceSwitcher(
                services=llm_services,
                strategy_type=_FailoverOnAnyLLMError,
            )

            @llm.strategy.event_handler("on_service_switched")
            async def _on_llm_switched(strategy, service):
                # Only ever fires on a real switch: the strategy's constructor
                # sets the initial active service without raising this.
                logger.warning(
                    f"LLM failed over to {service.name} — re-running this turn on it"
                )
                if task_holder[0] is not None:
                    await task_holder[0].queue_frames([LLMRunFrame()])

            async def _on_llm_error(service, error_frame):
                failover_left = llm_failover_retries[0] < _LLM_FAILOVER_MAX_RETRIES and any(
                    s is not service and s.is_usable for s in llm_services
                )
                if failover_left:
                    # A switch is coming; _on_llm_switched re-runs the turn, so
                    # staying quiet here is what lets the caller hear a real
                    # answer instead of an apology for a hiccup they never saw.
                    logger.warning(
                        f"LLM error on {service}: {error_frame.error} — failing over and retrying the turn"
                    )
                    return
                logger.warning(
                    f"LLM error on {service}: {error_frame.error} — no failover left, apologising to the caller"
                )
                if task_holder[0] is not None:
                    await task_holder[0].queue_frames([TTSSpeakFrame(_LLM_ERROR_RECOVERY.get(default_lang, _LLM_ERROR_RECOVERY["ur"]))])

            primary_llm.event_handler("on_error")(_on_llm_error)
            openai_llm_fallback.event_handler("on_error")(_on_llm_error)

        # Set whenever the caller says anything meaningful — end_call_handler
        # watches this to catch the LLM asking a question and calling end_call
        # in the same turn (a habit of the Groq-hosted models) without waiting
        # for the reply.
        caller_spoke_event = asyncio.Event()
        if not is_realtime:
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

        async def search_knowledge_base_handler(params: FunctionCallParams):
            """Realtime-mode-only tool: OpenAI Realtime has no separate
            RAGContextInjector pipeline stage (that stage sits between STT and
            the LLM, neither of which exist here), so retrieval is exposed as
            a callable tool instead — same ScriptRAG.retrieve() cascaded mode
            uses, called on-demand instead of injected every turn."""
            query = (params.arguments or {}).get("query", "")
            if not query or rag_task is None:
                await params.result_callback({"context": ""})
                return
            try:
                rag = await rag_task
            except Exception as exc:
                logger.warning(f"search_knowledge_base: RAG build failed: {exc}")
                await params.result_callback({"context": ""})
                return
            if rag is None:
                await params.result_callback({"context": ""})
                return
            context_text = await rag.retrieve(query, top_k=3)
            await params.result_callback({"context": context_text or ""})

        if not is_realtime:
            for _llm_service in (primary_llm, openai_llm_fallback):
                _llm_service.register_function("end_call", end_call_handler)
                _llm_service.register_function("check_caller_history", check_caller_history_handler)

        # Resolve the background history fetch here — this is the first place
        # the data is needed, so the DB round-trip overlapped with all the
        # service/pipeline setup above instead of delaying the greeting.
        if caller_history_task is not None:
            try:
                caller_history = await caller_history_task
            except Exception as exc:
                logger.warning(f"Caller history fetch failed — continuing without: {exc}")
                caller_history = []
        messages, lang_name = build_static_system_messages(system_prompt, default_lang, caller_history)
        logger.info(f"Caller history injected: {len(caller_history or [])} previous call(s)")
        end_call_tools = _build_end_call_tools(lang_name, include_search_tool=is_realtime and has_script)
        context = LLMContext(messages, tools=end_call_tools)
        if is_realtime:
            # ExternalUserTurnStrategies makes user_aggregator passive — it
            # won't run its own turn-start/stop detection or broadcast its own
            # interruptions. Necessary here: its default start strategy
            # includes TranscriptionUserTurnStartStrategy, which — now that
            # realtime sessions have transcription enabled (see
            # OpenAIRealtimeLLMSettings above) — fires on every incremental
            # transcription delta OpenAI streams, not just on genuine new
            # utterances. Each one broadcast a REDUNDANT interruption on top
            # of _RealtimeVADGate's (the one turn-detection signal this mode
            # actually needs), repeatedly cancelling OpenAI's in-progress
            # response before it could finish — root-caused via the same
            # Telnyx-protocol simulation as the earlier no-reply bug: replies
            # would start generating, then vanish, with
            # input_audio_buffer_commit_empty errors in the logs.
            user_turn_params = LLMUserAggregatorParams(
                user_turn_strategies=ExternalUserTurnStrategies(),
            )
        elif _USE_SMART_TURN:
            # EXPERIMENT (pipecat-upgrade branch) — semantic end-of-turn
            # detection instead of a fixed silence timeout: distinguishes
            # "caller paused to think" from "caller is actually done," the
            # way SpeechTimeoutUserTurnStopStrategy's flat timeout below
            # cannot. The known cost (per the comment this replaced): 1-4s of
            # added latency per turn, plus a model load — set against this
            # session's RAG/TTS work that clawed back ~3s of per-turn
            # latency elsewhere. Live-test via the Test Agent widget or a
            # real call before deciding whether the naturalness is worth it;
            # flip _USE_SMART_TURN back to False to revert instantly.
            #
            # vad_analyzer moved here (pipecat 1.0+): TransportParams.vad_analyzer
            # was removed and is now silently dropped by Pydantic if still passed
            # at the transport level (see per_call_transport_params in bot()) —
            # this is the only place it actually takes effect, feeding both
            # VADUserTurnStartStrategy (the default start strategy) and any
            # VAD-dependent stop strategy.
            user_turn_params = LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=stt_endpointing_ms / 1000)),
                user_turn_strategies=UserTurnStrategies(
                    stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
                ),
                user_turn_stop_timeout=_USER_TURN_STOP_FAILSAFE_SECS,
                user_idle_timeout=_USER_IDLE_TIMEOUT_SECS,
            )
        else:
            # Faster turn-taking: the default stop strategy runs the semantic Smart Turn
            # model, which adds 1-4s of "has the caller finished?" latency per turn (and
            # loads a model per call). Replace it with a pure VAD-timeout stop so the bot
            # responds as soon as the caller pauses.
            user_turn_params = LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=stt_endpointing_ms / 1000)),
                user_turn_strategies=UserTurnStrategies(
                    # wait_for_transcript stays at its default (True) on
                    # purpose. It was briefly set False here to dodge a turn
                    # that never fired, but that traded one failure for a worse
                    # one: the strategy's own timers expire ~0.79s after VAD
                    # stop (GROQ_TTFS_P99 1.54s minus our 0.75s stop_secs)
                    # while Groq actually returns the transcript ~0.66-0.82s
                    # after VAD stop — so the turn kept firing a few ms BEFORE
                    # the text landed, sending an empty turn to the LLM and
                    # deferring the caller's real question to the next turn
                    # cycle (measured live: 15.7s from question to reply).
                    # Left True, _handle_transcription fires the stop the
                    # instant the transcript arrives once both timers are done
                    # — no fixed wait, and the turn always carries its text.
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
                ),
                # Failsafe for a turn whose transcript never arrives at all.
                # Pipecat's own default is 5.0s; every healthy turn observed
                # resolved within ~0.6-2.6s, so capping this well below 5s
                # bounds the dead air without touching normal-path latency.
                user_turn_stop_timeout=_USER_TURN_STOP_FAILSAFE_SECS,
                user_idle_timeout=_USER_IDLE_TIMEOUT_SECS,
            )
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context, user_params=user_turn_params
        )

        if not is_realtime:
            # Re-engage a caller who has gone quiet (see _IDLE_NUDGES). Speaking
            # here re-arms pipecat's idle timer on the next BotStoppedSpeaking,
            # so this fires again if they stay silent — the counter is what
            # stops it after _IDLE_MAX_NUDGES instead of nudging indefinitely.
            idle_nudges_sent = [0]

            @user_aggregator.event_handler("on_user_turn_idle")
            async def _on_user_turn_idle(aggregator):
                if task_holder[0] is None:
                    return
                sent = idle_nudges_sent[0]
                if sent >= _IDLE_MAX_NUDGES:
                    logger.info(f"Caller idle after {sent} nudge(s) — closing the call politely")
                    await task_holder[0].queue_frames(
                        [TTSSpeakFrame(_IDLE_GIVE_UP.get(default_lang, _IDLE_GIVE_UP["ur"]))]
                    )
                    await asyncio.sleep(4)  # let the closing line actually play
                    if hangup_callback is not None:
                        await hangup_callback()
                    await task_holder[0].cancel()
                    return
                idle_nudges_sent[0] = sent + 1
                nudges = _IDLE_NUDGES.get(default_lang, _IDLE_NUDGES["ur"])
                logger.info(f"Caller silent for {_USER_IDLE_TIMEOUT_SECS}s — nudge {sent + 1}/{_IDLE_MAX_NUDGES}")
                await task_holder[0].queue_frames([TTSSpeakFrame(nudges[min(sent, len(nudges) - 1)])])

            @user_aggregator.event_handler("on_user_turn_started")
            async def _reset_idle_nudges(aggregator, strategy):
                # The caller came back — start the nudge budget over so a later
                # pause in a long call isn't judged by earlier silences.
                idle_nudges_sent[0] = 0
                # Same reasoning for the failover budget: it exists to stop two
                # unhappy providers trading ONE turn back and forth, so it is
                # scoped to a turn. Without this reset a call that hiccuped
                # early would apologise for every later error instead of
                # failing over.
                llm_failover_retries[0] = 0

        if is_realtime:
            # No cached-PCM greeting fast path here (that assumes a distinct
            # TTS stage — see run_bot's greeting comment below) — the greeting
            # is baked into instructions instead, and the model speaks it as
            # its own first real turn so OpenAI's server-side session state
            # (which this service, not this pipeline, tracks) knows it happened.
            agent_name = (agent.get("name") or "") if agent else ""
            if is_outbound:
                _rt_greetings = {
                    "ur": f"السلام علیکم! {agent_name} کی طرف سے آپ کو کال کی جا رہی ہے۔ کیا آپ کے پاس چند لمحے ہیں؟",
                    "en": f"Hello! This is {agent_name} calling. Do you have a moment to talk?",
                }
                rt_greeting_text = _rt_greetings.get(default_lang, _rt_greetings["ur"])
            else:
                # Same custom-or-default resolution as the cached-PCM path
                # below uses for cascaded mode — an agent's greeting_text
                # override must apply here too, not just in cascaded calls.
                _, _, _, _, rt_greeting_text, _ = await resolve_inbound_greeting(agent)

            instructions = "\n\n".join(m["content"] for m in messages)
            conv_msg = build_conv_state_message(extraction_fields, lang_name)
            if conv_msg:
                instructions += "\n\n" + conv_msg["content"]
            instructions += (
                f"\n\nAs soon as the session starts, greet the caller immediately in "
                f"{lang_name} with exactly: '{rt_greeting_text}'. Do not wait for the "
                f"caller to speak first."
            )

            # Grok Voice reuses this same service unmodified — xAI's Voice
            # Agent API documents the same WebSocket protocol shape OpenAI's
            # Realtime API uses (session.update, base64 audio deltas, a
            # `?model=` query param, `Authorization: Bearer` auth — and
            # pipecat's own _connect() sends nothing OpenAI-specific beyond
            # that Bearer header), so only base_url/api_key/model differ per
            # provider (see REALTIME_PROVIDERS). Unverified against a real
            # xAI key — verify empirically before trusting it for live calls.
            realtime_session_properties = SessionProperties(
                audio=AudioConfiguration(
                    # turn_detection=False: OpenAI's own server-side VAD
                    # proved unreliable specifically over the Telnyx
                    # resampled-to-24kHz audio path — it detects speech
                    # STARTING fine (interruption fires) but never
                    # reliably auto-creates a reply, i.e. never detects
                    # the caller has STOPPED (root-caused via a direct
                    # Telnyx-protocol simulation, not just guessed).
                    # _RealtimeVADGate (added to the pipeline below)
                    # drives turn-taking explicitly instead, using the
                    # same Silero VAD proven reliable everywhere else
                    # in this system, on its own resampled 16kHz copy
                    # of the audio (Silero can't run at 24kHz directly).
                    input=AudioInput(
                        format=PCMAudioFormat(),
                        turn_detection=False,
                        # transcription defaults to None (disabled) —
                        # without it OpenAI never emits the
                        # TranscriptionFrame ConversationLogger needs
                        # for USER-turn logging (see conversation_logger.py),
                        # even though the model still understands the
                        # caller's audio fine either way for its own
                        # replies. language hint uses the same
                        # LANGUAGE_WHISPER_MAP cascaded mode's STT uses.
                        transcription=InputAudioTranscription(
                            language=LANGUAGE_WHISPER_MAP.get(default_lang, "ur"),
                            prompt=None,
                        ),
                    ),
                    output=AudioOutput(format=PCMAudioFormat(), voice=realtime_voice),
                ),
                tools=end_call_tools,
            )
            # model is passed only when the provider needs a non-default one
            # (Grok) — the settings object's sync logic only runs when a
            # field is passed at construction time (via apply_update(), see
            # OpenAIRealtimeLLMService.__init__), not on a later attribute
            # assignment, and passing model=None explicitly (instead of
            # omitting it) is NOT equivalent to leaving it unset — pipecat
            # distinguishes "not given" (its own default applies) from an
            # explicit None (which would overwrite the default with a
            # literal "None" model string in the connect URL). Omitting the
            # kwarg entirely is what lets OpenAI mode keep pipecat's own
            # tested default model.
            realtime_settings_kwargs = {
                "system_instruction": instructions,
                "session_properties": realtime_session_properties,
            }
            if realtime_provider["model"]:
                realtime_settings_kwargs["model"] = realtime_provider["model"]

            realtime_llm = OpenAIRealtimeLLMService(
                api_key=os.getenv(realtime_provider["api_key_env"], ""),
                base_url=realtime_provider["base_url"],
                settings=OpenAIRealtimeLLMSettings(**realtime_settings_kwargs),
            )
            realtime_llm.register_function("end_call", end_call_handler)
            realtime_llm.register_function("check_caller_history", check_caller_history_handler)
            if has_script and rag_task is not None:
                realtime_llm.register_function("search_knowledge_base", search_knowledge_base_handler)
            llm = realtime_llm

        rtvi = RTVIProcessor()
        call_id = uuid.uuid4().hex[:8]
        convo_logger = ConversationLogger(
            call_id=call_id,
            db_call_id=db_call_id,
            call_control_id=call_control_id,
        )
        metrics_collector = CallMetricsCollector(
            db_call_id=db_call_id,
            realtime_provider=pipeline_mode if is_realtime else None,
            stt_provider=stt_provider,
        )

        # RAGContextInjector (cascaded mode) resolves rag_task lazily on the
        # first caller turn rather than being awaited here, so the RAG build
        # (~5-8s on a cold cache) never delays the opening greeting. Realtime
        # mode's equivalent is the search_knowledge_base tool registered above,
        # which awaits rag_task itself only when the model actually calls it.
        audio_probe = _AudioFrameProbe()

        if is_realtime:
            # No STT/TTS stages — OpenAIRealtimeLLMService handles audio
            # in/out directly (see pipecat's own recommended wiring).
            pipeline_stages = [
                transport.input(),
                audio_probe,
                rtvi,
                user_aggregator,
                _RealtimeVADGate(),
                llm,
            ]
            if browser_event_dedup is not None:
                # Catches this branch's TranscriptionFrame — OpenAIRealtimeLLMService
                # emits it right here, downstream of user_aggregator (see
                # _BrowserEventBridge's docstring for why it needs re-wrapping).
                pipeline_stages.append(_BrowserEventBridge(FrameDirection.DOWNSTREAM, browser_event_dedup))
            pipeline_stages.extend([
                _RealtimeOutputSmoother(),
                transport.output(),
            ])
            if browser_event_dedup is not None:
                # Catches BotStarted/StoppedSpeaking, created by transport.output()
                # itself — must sit after it and push back UPSTREAM.
                pipeline_stages.append(_BrowserEventBridge(FrameDirection.UPSTREAM, browser_event_dedup))
            pipeline_stages.append(assistant_aggregator)
        else:
            pipeline_stages = [
                transport.input(),
                audio_probe,
                rtvi,
                stt,
                noise_filter,
                LongNumberAccumulator(),
            ]
            if browser_event_dedup is not None:
                # Catches UserStarted/StoppedSpeaking and — before user_aggregator
                # absorbs it — TranscriptionFrame (see _BrowserEventBridge's
                # docstring; the caller's turn surfaces again via LLMContextFrame
                # once it reaches the post-transport.output() instance below).
                pipeline_stages.append(_BrowserEventBridge(FrameDirection.DOWNSTREAM, browser_event_dedup))
            pipeline_stages.append(user_aggregator)
            if has_script and rag_task is not None:
                pipeline_stages.append(RAGContextInjector(
                    rag_task=rag_task, top_k=3, extraction_fields=extraction_fields,
                    response_language=lang_name,
                ))
            pipeline_stages.extend([
                llm,
                tts,
                transport.output(),
            ])
            if browser_event_dedup is not None:
                # Catches BotStarted/StoppedSpeaking (created by transport.output()
                # itself), TTSTextFrame, and LLMContextFrame (the caller's
                # accumulated turn) — must sit after transport.output() and
                # push back UPSTREAM.
                pipeline_stages.append(_BrowserEventBridge(FrameDirection.UPSTREAM, browser_event_dedup))
            pipeline_stages.append(assistant_aggregator)

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

        async def _audio_watchdog():
            await asyncio.sleep(_AUDIO_WATCHDOG_DELAY_SECS)
            if audio_probe.count > 0:
                return  # audio is flowing — nothing to do
            logger.warning(
                f"No inbound audio received {_AUDIO_WATCHDOG_DELAY_SECS}s after connect "
                f"(ccid={call_control_id}, outbound={is_outbound}) — likely a carrier-side "
                f"one-way-audio issue this app can't repair; ending the call instead of "
                f"leaving the caller in dead air."
            )
            await task.queue_frames([TTSSpeakFrame(_NO_AUDIO_APOLOGY.get(default_lang, _NO_AUDIO_APOLOGY["ur"]))])
            await asyncio.sleep(4)  # let the apology actually finish playing
            if hangup_callback is not None:
                await hangup_callback()
            await task.cancel()

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Client connected (outbound={is_outbound}, realtime={is_realtime})")
            # Cascaded calls only — realtime's TTS is embedded in the LLM
            # service itself, so a bare TTSSpeakFrame here wouldn't be spoken.
            # hangup_callback is None for the dashboard's Test Agent widget
            # (app/api/agent_test.py) — never watch audio on that path, it
            # isn't a real Telnyx media stream.
            if not is_realtime and hangup_callback is not None:
                asyncio.create_task(_audio_watchdog())
            if is_realtime:
                # The greeting is already baked into realtime_llm's instructions
                # (built above) — pushing the initial context frame is what
                # triggers OpenAIRealtimeLLMService to generate its first
                # response (see _handle_context in pipecat's realtime service:
                # it auto-calls _create_response() the first time it receives
                # a context). No cached-PCM injection here — unlike cascaded
                # mode, that audio wouldn't exist in OpenAI's own server-side
                # session state, so the model wouldn't know it already greeted.
                await task.queue_frames([LLMContextFrame(context=context)])
                return
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
                g_engine, g_voice, g_key, g_model, greeting_text, g_speed = await resolve_inbound_greeting(agent)
                # Add to context as assistant message so LLM doesn't re-greet
                messages.append({"role": "assistant", "content": greeting_text})

                cached = await get_greeting_pcm(
                    g_engine, g_voice, greeting_text,
                    api_key=g_key, session=session, model=g_model, speed=g_speed,
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
    caller_history_task: asyncio.Task | None = None,
    caller_phone: str | None = None,
    user_id: str = "",
):
    """Main bot entry point compatible with Pipecat Cloud."""
    # pipecat 1.0+: VAD is no longer configured on transport params (removed
    # field, silently dropped by Pydantic if still passed here — see
    # run_bot()'s LLMUserAggregatorParams, the only place vad_analyzer takes
    # effect now). Transport params are just audio in/out enablement.
    per_call_transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
        "telnyx": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    transport = await create_transport(runner_args, per_call_transport_params)
    await run_bot(
        transport,
        runner_args,
        hangup_callback=hangup_callback,
        is_outbound=is_outbound,
        agent=agent,
        db_call_id=db_call_id,
        call_control_id=call_control_id,
        caller_history=caller_history,
        caller_history_task=caller_history_task,
        caller_phone=caller_phone,
        user_id=user_id,
    )


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
