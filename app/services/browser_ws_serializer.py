"""Frame serializer for the agent-test widget's browser WebSocket.

Same role as pipecat's TelnyxFrameSerializer/TwilioFrameSerializer, but for a
client we control end-to-end instead of a telephony provider — so the wire
format is deliberately minimal: raw 16kHz mono 16-bit PCM binary frames for
audio (no base64/JSON wrapping, no resampling), and small JSON text messages
for everything else (transcripts, bot text, interruption/barge-in signals).

This is what lets the dashboard's "Talk To Your Agent" widget run the exact
same pipecat pipeline a live Telnyx call uses (proper Silero VAD, streaming
STT/LLM/TTS) — see app/api/agent_test.py's /ws/agent-test route — instead of
the turn-based HTTP approximation, which could never be genuinely real-time
no matter how much it was tuned.
"""

import json

from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMContextFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer

SAMPLE_RATE = 16000


def _build_event_message(frame: Frame, dedup: dict) -> dict | None:
    """Build this widget's JSON event dict for a frame, or None if this frame
    type isn't one of ours. `dedup` carries {"last_user_sent": str} — a plain
    dict (not an instance attribute) so both the serializer and
    _BrowserEventBridge below can share one dedup state per connection.

    RAGContextInjector re-emits LLMContextFrame with the same user turn
    already folded in, which would otherwise send the caller's line twice.
    """
    if isinstance(frame, InterruptionFrame):
        return {"event": "interruption"}
    if isinstance(frame, UserStartedSpeakingFrame):
        return {"event": "user_started_speaking"}
    if isinstance(frame, UserStoppedSpeakingFrame):
        return {"event": "user_stopped_speaking"}
    if isinstance(frame, BotStartedSpeakingFrame):
        return {"event": "bot_started_speaking"}
    if isinstance(frame, BotStoppedSpeakingFrame):
        return {"event": "bot_stopped_speaking"}
    if isinstance(frame, TranscriptionFrame):
        # Realtime mode: OpenAIRealtimeLLMService pushes its own
        # TranscriptionFrame once it's transcribed the caller's audio. In
        # cascaded mode this never fires — pipecat's LLMUserContextAggregator
        # consumes the STT's TranscriptionFrame (see LLMContextFrame below).
        text = (frame.text or "").strip()
        if not text or text == dedup["last_user_sent"]:
            return None
        dedup["last_user_sent"] = text
        return {"event": "transcript", "role": "user", "text": text}
    if isinstance(frame, LLMContextFrame):
        # Cascaded mode: the STT's TranscriptionFrame is absorbed by
        # user_aggregator (see above), so the caller's turn only surfaces
        # again here, folded into the accumulated context — same source
        # ConversationLogger reads for the USER line in transcript_turns.
        try:
            messages = frame.context.get_messages()
        except Exception:
            return None
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                return None
            content = content.strip()
            if not content or content == dedup["last_user_sent"]:
                return None
            dedup["last_user_sent"] = content
            return {"event": "transcript", "role": "user", "text": content}
        return None
    if isinstance(frame, TTSTextFrame):
        return {"event": "bot_text", "text": frame.text}
    return None


class _BrowserEventBridge(FrameProcessor):
    """Re-wraps this widget's UI events as OutputTransportMessageUrgentFrame
    so they actually reach the browser.

    pipecat 0.0.108's FastAPIWebsocketOutputTransport has no fallback path
    for a frame type it doesn't specifically recognize (audio/image/output
    message/DTMF) — anything else reaching transport.output() (confirmed by
    direct server-side testing: BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
    TranscriptionFrame, TTSTextFrame, LLMContextFrame) silently calls a
    write_transport_frame() method that doesn't exist on this transport and
    is dropped. OutputTransportMessageUrgentFrame is the one type that IS
    unconditionally handled regardless of direction, so re-wrapping our
    events in it and pushing them toward transport.output() is what actually
    gets them onto the wire.

    Two instances sit in the pipeline (see app/services/bot.py) — one before
    transport.output() (catches UserStarted/StoppedSpeaking, TranscriptionFrame,
    LLMContextFrame; pushes DOWNSTREAM toward it) and one after it (catches
    BotStarted/StoppedSpeaking, TTSTextFrame — created by transport.output()
    itself or by tts just before it; pushes UPSTREAM back to it), since no
    single position in the pipeline sees every event type. Every input frame
    is still forwarded unchanged in its original direction regardless — this
    only ever ADDS a message frame alongside it.
    """

    def __init__(self, push_direction: FrameDirection, dedup: dict):
        super().__init__()
        self._push_direction = push_direction
        self._dedup = dedup

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        message = _build_event_message(frame, self._dedup)
        if message is not None:
            await self.push_frame(
                OutputTransportMessageUrgentFrame(message=message), self._push_direction,
            )
        await self.push_frame(frame, direction)


class BrowserFrameSerializer(FrameSerializer):
    def __init__(self):
        super().__init__()
        self._sample_rate = SAMPLE_RATE
        # Shared with _BrowserEventBridge instances built alongside this
        # serializer in bot.py — see _build_event_message's docstring.
        self.dedup = {"last_user_sent": ""}

    async def setup(self, frame: StartFrame):
        self._sample_rate = frame.audio_in_sample_rate or SAMPLE_RATE

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, AudioRawFrame):
            return frame.audio

        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)

        # Everything else reaching serialize() directly is the confirmed-dead
        # path described in _BrowserEventBridge's docstring — kept as a
        # harmless fallback in case a future pipecat version restores it.
        message = _build_event_message(frame, self.dedup)
        return json.dumps(message) if message is not None else None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            if not data:
                return None
            return InputAudioRawFrame(audio=data, num_channels=1, sample_rate=self._sample_rate)

        try:
            message = json.loads(data)
        except ValueError:
            return None

        if message.get("event") == "end":
            logger.info("BrowserFrameSerializer: client requested end")
        return None
