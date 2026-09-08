#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Uplift TTS service integration."""

import asyncio
import base64
import contextlib
import uuid
from typing import AsyncGenerator, Optional

import aiohttp
import socketio
from loguru import logger
from pydantic import BaseModel

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.utils.tracing.service_decorators import traced_tts


class UpliftHttpTTSService(TTSService):
    """Uplift HTTP TTS service implementation.

    This service provides text-to-speech synthesis using the Uplift HTTP API.

    Supported voices:
        Urdu: v_8eelc901 (Info/Edu), v_kwmp7zxt (Gen Z),
              v_yypgzenx (Dada Jee), v_30s70t3a (Nostalgic News)
        Sindhi: v_sd0kl3m9
        Balochi/English: use Urdu voices as fallback.

    Supported output formats:
        - WAV_22050_16, WAV_22050_32
        - MP3_22050_32, MP3_22050_64, MP3_22050_128
        - OGG_22050_16
        - ULAW_8000_8
    """

    # Available voices
    AVAILABLE_VOICES = [
        # Urdu voices (4 style variants)
        "v_8eelc901",  # Info/Edu — clear educational tone (default)
        "v_kwmp7zxt",  # Gen Z — casual modern style
        "v_yypgzenx",  # Dada Jee — traditional respectful tone
        "v_30s70t3a",  # Nostalgic News — classic news anchor
        # Regional voices
        "v_sd0kl3m9",  # Sindhi
        "v_bl1de2f7",  # Balochi
    ]

    # Available output formats
    AVAILABLE_FORMATS = [
        "WAV_22050_16",
        "WAV_22050_32",
        "MP3_22050_32",
        "MP3_22050_64",
        "MP3_22050_128",
        "OGG_22050_16",
        "ULAW_8000_8",
    ]

    class InputParams(BaseModel):
        """Optional input parameters for Uplift TTS configuration.

        Parameters:
            voice_id: Optional default voice ID override.
            output_format: Audio output format. Defaults to WAV_22050_16.
        """

        voice_id: Optional[str] = None
        output_format: Optional[str] = "WAV_22050_16"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.upliftai.org/v1/synthesis/text-to-speech",
        voice_id: str = "v_8eelc901",
        output_format: str = "WAV_22050_16",
        sample_rate: int = 22050,
        aiohttp_session: Optional[aiohttp.ClientSession] = None,
        params: Optional[InputParams] = None,
        **kwargs,
    ):
        """Initializes the Uplift HTTP TTS service.

        Args:
            api_key: Uplift API key for authentication (format: "sk_api_...").
            base_url: Base URL for the Uplift TTS API endpoint.
            voice_id: Uplift voice identifier. Available voices:
                - v_8eelc901 (Info/Edu)
                - v_kwmp7zxt (Gen Z)
                - v_yypgzenx (Dada Jee)
                - v_30s70t3a (Nostalgic News)
            output_format: Audio output format. Defaults to WAV_22050_16.
                Available formats: WAV_22050_16, WAV_22050_32, MP3_22050_32,
                MP3_22050_64, MP3_22050_128, OGG_22050_16, ULAW_8000_8
            sample_rate: Audio sample rate in Hz. Defaults to 22050.
            aiohttp_session: Optional aiohttp session for making requests.
            params: Voice customization parameters.
            **kwargs: Additional arguments passed to parent TTSService.

        Raises:
            ValueError: If api_key is not provided or invalid settings.
        """
        effective_voice = (params.voice_id if params and params.voice_id else voice_id)
        super().__init__(
            sample_rate=sample_rate,
            settings=TTSSettings(model=None, voice=effective_voice, language=None),
            **kwargs,
        )

        if not api_key:
            raise ValueError("Missing Uplift API key")

        self._api_key = api_key
        self._base_url = base_url
        self._params = params or UpliftHttpTTSService.InputParams()

        # Session management: create session if not provided
        self._session = aiohttp_session
        self._created_session = False
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._created_session = True
            logger.debug("Created internal aiohttp session")

        # Validate and set output format
        final_output_format = self._params.output_format or output_format
        if final_output_format not in self.AVAILABLE_FORMATS:
            logger.warning(
                f"Output format '{final_output_format}' not in known formats list. "
                f"Available formats: {', '.join(self.AVAILABLE_FORMATS)}"
            )

        self._uplift_config = {
            "voice_id": self._params.voice_id or voice_id,
            "output_format": final_output_format,
        }

        if self._uplift_config["voice_id"] not in self.AVAILABLE_VOICES:
            logger.warning(
                f"Voice '{self._uplift_config['voice_id']}' not in known voices list. "
                f"Available voices: {', '.join(self.AVAILABLE_VOICES)}"
            )

        self.set_voice(self._uplift_config["voice_id"])

        logger.debug(
            f"Uplift HTTP TTS initialized with voice_id={self._uplift_config['voice_id']}, "
            f"output_format={self._uplift_config['output_format']}"
        )

    async def start(self, frame: StartFrame):
        """Start the service and initialize sample rate from pipeline."""
        await super().start(frame)
        logger.debug(f"Uplift TTS started with sample_rate={self.sample_rate} Hz")

    def can_generate_metrics(self) -> bool:
        return True

    async def set_voice_id(self, voice_id: str):
        """Set the voice ID for TTS generation."""
        if voice_id not in self.AVAILABLE_VOICES:
            logger.warning(
                f"Voice '{voice_id}' not in known voices list. "
                f"Available voices: {', '.join(self.AVAILABLE_VOICES)}"
            )
        logger.info(f"Switching Uplift TTS voice to: [{voice_id}]")
        self._uplift_config["voice_id"] = voice_id
        self.set_voice(voice_id)

    async def set_output_format(self, output_format: str):
        """Set the output audio format."""
        if output_format not in self.AVAILABLE_FORMATS:
            logger.warning(
                f"Output format '{output_format}' not in known formats list. "
                f"Available formats: {', '.join(self.AVAILABLE_FORMATS)}"
            )
        logger.info(f"Switching Uplift TTS output format to: [{output_format}]")
        self._uplift_config["output_format"] = output_format

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

    async def cleanup(self):
        if self._created_session and self._session:
            await self._session.close()
            self._session = None
            logger.debug("Closed internal aiohttp session")

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Generate speech from text using Uplift's TTS endpoint."""
        logger.debug(f"{self}: Generating TTS [{text}]")

        if len(text) > 2500:
            logger.warning(
                f"Text length ({len(text)}) exceeds Uplift's maximum of 2500 characters. "
                f"Truncating..."
            )
            text = text[:2500]

        try:
            await self.start_ttfb_metrics()

            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }

            payload = {
                "text": text,
                "voiceId": self._uplift_config["voice_id"],
                "outputFormat": self._uplift_config["output_format"],
            }

            async with self._session.post(
                self._base_url, json=payload, headers=headers
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    error_message = f"Uplift HTTP TTS error: {response.status} - {error_text}"
                    logger.error(error_message)
                    await self.push_error(ErrorFrame(error=error_message))
                    yield ErrorFrame(error=error_message)
                    return

                audio_bytes = await response.read()

            await self.stop_ttfb_metrics()
            await self.start_tts_usage_metrics(text)

            if self._uplift_config["output_format"].startswith("WAV_"):
                audio_content = audio_bytes[44:]
            else:
                audio_content = audio_bytes

            yield TTSAudioRawFrame(audio_content, self.sample_rate, 1, context_id=context_id)

        except Exception as e:
            error_message = f"TTS generation error: {str(e)}"
            logger.error(error_message)
            await self.push_error(ErrorFrame(error=error_message))
            yield ErrorFrame(error=error_message)


class UpliftStreamingTTSService(TTSService):
    """Uplift AI streaming TTS service.

    Primary path: Uplift's WebSocket multi-stream API (Socket.IO), which
    Uplift documents at ~300ms first-chunk latency vs. ~1.3-2s measured live
    on the plain HTTP streaming endpoint this class used to call exclusively
    — the HTTP endpoint pays a fresh TCP+TLS handshake and a from-scratch
    server-side synthesis request every single turn, where the WS path opens
    one persistent connection for the whole call (connected in start(),
    overlapping with greeting playback so it's warm before the caller's
    first turn) and reuses it turn after turn.

    Falls back to the original HTTP endpoint whenever the WS connection
    isn't up yet, or a WS request stalls/errors before any audio reaches the
    caller — Uplift's streaming endpoint has a documented history of rare
    mid-call stalls (see the TTS engine selection comment in bot.py), so this
    keeps that safety net rather than trusting the new path unconditionally.

    Voices: Urdu (v_8eelc901 / v_kwmp7zxt / v_yypgzenx / v_30s70t3a);
    English falls back to the Urdu default voice.
    """

    _STREAM_URL = "https://api.upliftai.org/v1/synthesis/text-to-speech/stream"

    # Uplift streaming has no library-level timeout, so a hung connection
    # would otherwise stall a call turn for aiohttp's long default. sock_read
    # aborts if the stream stalls between chunks; total caps the worst case.
    # Tightened from (10, 30): a real stall was observed taking ~19s to abort
    # and retry — long enough that the caller hung up before the successful
    # retry's audio arrived. A healthy stream starts returning chunks in well
    # under a second, so failing (and retrying) fast is strictly better than
    # waiting out a long timeout on a connection that's already stuck.
    _REQUEST_TIMEOUT = aiohttp.ClientTimeout(connect=5, sock_connect=5, sock_read=5, total=12)
    _MAX_ATTEMPTS = 2

    _WS_BASE_URL = "https://api.upliftai.org"
    _WS_NAMESPACE = "/text-to-speech/multi-stream"
    _WS_CONNECT_TIMEOUT = 5.0
    # No chunk is expected to take this long once a request is accepted —
    # matches the same "fail fast, fall back" philosophy as _REQUEST_TIMEOUT.
    _WS_EVENT_TIMEOUT = 6.0

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str = "v_8eelc901",
        speed: float = 1.0,
        aiohttp_session: aiohttp.ClientSession,
        sample_rate: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            settings=TTSSettings(model=None, voice=voice_id, language=None),
            **kwargs,
        )

        self._api_key = api_key
        self._session = aiohttp_session
        self._uplift_config = {
            "voice_id": voice_id,
            "output_format": "WAV_22050_16",
            # Confirmed live: Uplift's stream endpoint honors this undocumented
            # "speed" field (measurably shorter/longer output audio at
            # different values) — not sent at all when 1.0 (normal), same as
            # the ElevenLabs branch only sending voice_settings.speed when
            # non-default.
            "speed": speed,
        }

        self._sio: Optional[socketio.AsyncClient] = None
        self._sio_usable = False
        self._sio_ready_event = asyncio.Event()
        self._sio_pending: dict[str, asyncio.Queue] = {}
        self._sio_connect_task: Optional[asyncio.Task] = None

    def can_generate_metrics(self) -> bool:
        return True

    async def set_voice_id(self, voice_id: str) -> None:
        """Switch the active voice at runtime."""
        logger.info(f"Switching Uplift streaming TTS voice to: [{voice_id}]")
        self._uplift_config["voice_id"] = voice_id

    async def start(self, frame: StartFrame):
        """Open the WS connection in the background — overlaps with greeting
        playback/RAG build instead of adding to any single turn's latency."""
        await super().start(frame)
        self._sio_connect_task = asyncio.create_task(self._connect_ws())

    async def cleanup(self):
        """Tear down the WS connection at call end."""
        await super().cleanup()
        if self._sio_connect_task and not self._sio_connect_task.done():
            self._sio_connect_task.cancel()
        if self._sio is not None:
            with contextlib.suppress(Exception):
                await self._sio.disconnect()
            self._sio = None

    @property
    def _ws_ready(self) -> bool:
        return self._sio_usable and self._sio is not None and self._sio.connected

    async def _on_ws_message(self, data: dict) -> None:
        if data.get("type") == "ready":
            self._sio_ready_event.set()
            return
        request_id = data.get("requestId")
        queue = self._sio_pending.get(request_id) if request_id else None
        if queue is not None:
            queue.put_nowait(data)

    async def _connect_ws(self) -> None:
        # Assigned to self._sio immediately (before the handshake even
        # starts) so cleanup() can always close it — including the edge case
        # where the call ends while this task is still cancelled mid-connect,
        # which would otherwise leak an open connection on Uplift's side.
        # self._sio_usable (checked by _ws_ready) only flips once the
        # session is actually confirmed ready to accept synthesize requests.
        sio = socketio.AsyncClient(reconnection=True, reconnection_attempts=5, reconnection_delay=1)
        self._sio = sio
        sio.on("message", self._on_ws_message, namespace=self._WS_NAMESPACE)
        try:
            await sio.connect(
                self._WS_BASE_URL,
                auth={"token": self._api_key},
                transports=["websocket"],
                namespaces=[self._WS_NAMESPACE],
                wait_timeout=self._WS_CONNECT_TIMEOUT,
            )
            await asyncio.wait_for(self._sio_ready_event.wait(), timeout=self._WS_CONNECT_TIMEOUT)
            self._sio_usable = True
            logger.debug("Uplift WS TTS connected and ready")
        except asyncio.TimeoutError:
            logger.warning("Uplift WS TTS: connected but no 'ready' message — using HTTP for this call")
        except Exception as exc:
            logger.warning(f"Uplift WS TTS connect failed — using HTTP for this call: {exc}")

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        # The LLM occasionally emits punctuation-only sentences (".." / "."),
        # which Uplift would happily "speak" as odd noises — skip them.
        if not any(ch.isalnum() for ch in text):
            logger.debug(f"Skipping punctuation-only TTS text: {text!r}")
            return

        if len(text) > 2500:
            logger.warning(f"Text too long ({len(text)} chars) — truncating to 2500")
            text = text[:2500]

        await self.start_ttfb_metrics()

        if self._ws_ready:
            async for frame in self._run_tts_ws(text, context_id):
                yield frame
        else:
            async for frame in self._run_tts_http(text, context_id):
                yield frame

    async def _run_tts_ws(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """WebSocket path — persistent connection, one synthesize per turn."""
        request_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        self._sio_pending[request_id] = queue
        logger.debug(f"{self}: Generating TTS via WS [{text}]")

        payload = {
            "type": "synthesize",
            "requestId": request_id,
            "text": text,
            "voiceId": self._uplift_config["voice_id"],
            "outputFormat": "PCM_22050_16",
        }
        if self._uplift_config["speed"] != 1.0:
            payload["speed"] = self._uplift_config["speed"]

        got_audio = False
        needs_fallback = False
        try:
            await self._sio.emit("synthesize", payload, namespace=self._WS_NAMESPACE)
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=self._WS_EVENT_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning(f"Uplift WS TTS stalled (no event within {self._WS_EVENT_TIMEOUT}s)")
                    needs_fallback = True
                    break

                msg_type = data.get("type")
                if msg_type == "audio":
                    if not got_audio:
                        await self.stop_ttfb_metrics()
                        await self.start_tts_usage_metrics(text)
                    got_audio = True
                    yield TTSAudioRawFrame(
                        base64.b64decode(data["audio"]), self.sample_rate, 1, context_id=context_id
                    )
                elif msg_type == "audio_end":
                    return  # success
                elif msg_type == "error":
                    logger.error(f"Uplift WS TTS error: {data.get('message')}")
                    needs_fallback = True
                    break
                # audio_start carries nothing we need — just keep waiting.
        except asyncio.CancelledError:
            # Caller interrupted (barge-in) — best-effort tell Uplift to stop
            # generating/billing for an utterance no one will hear.
            with contextlib.suppress(Exception):
                await self._sio.emit("cancel", {"type": "cancel", "requestId": request_id}, namespace=self._WS_NAMESPACE)
            raise
        finally:
            self._sio_pending.pop(request_id, None)

        if not needs_fallback:
            return

        # Only safe to retry via HTTP when nothing has reached the caller yet
        # — retrying mid-utterance would replay part of it twice.
        with contextlib.suppress(Exception):
            await self._sio.emit("cancel", {"type": "cancel", "requestId": request_id}, namespace=self._WS_NAMESPACE)
        if got_audio:
            await self.stop_ttfb_metrics()
            yield ErrorFrame(error="Uplift WS TTS stream ended unexpectedly mid-utterance")
            return
        logger.warning("Falling back to Uplift HTTP streaming for this utterance")
        async for frame in self._run_tts_http(text, context_id):
            yield frame

    async def _run_tts_http(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Plain HTTP streaming fallback — used when the WS connection isn't
        up yet, or a WS request stalled/errored before any audio was sent."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(f"{self}: Generating TTS via HTTP [{text}]")

        payload = {
            "text": text,
            "voiceId": self._uplift_config["voice_id"],
            "outputFormat": self._uplift_config["output_format"],
        }
        if self._uplift_config["speed"] != 1.0:
            payload["speed"] = self._uplift_config["speed"]

        error_message = ""

        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            yielded_audio = False
            try:
                async with self._session.post(
                    self._STREAM_URL, json=payload, headers=headers, timeout=self._REQUEST_TIMEOUT
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        error_message = f"Uplift streaming TTS error: {response.status} - {error_text}"
                        logger.error(f"{error_message} (attempt {attempt}/{self._MAX_ATTEMPTS})")
                    else:
                        await self.start_tts_usage_metrics(text)
                        async for frame in self._stream_audio_frames_from_iterator(
                            response.content.iter_chunked(self.chunk_size),
                            strip_wav_header=True,
                            context_id=context_id,
                        ):
                            yielded_audio = True
                            await self.stop_ttfb_metrics()
                            yield frame
                        await self.stop_ttfb_metrics()
                        return  # success
            except Exception as e:
                error_message = f"TTS error: {str(e)}"
                logger.error(f"{error_message} (attempt {attempt}/{self._MAX_ATTEMPTS})")

            # Only retry a clean failure before any audio reached the caller —
            # retrying mid-stream would replay part of the utterance twice.
            if yielded_audio or attempt >= self._MAX_ATTEMPTS:
                await self.stop_ttfb_metrics()
                yield ErrorFrame(error=error_message)
                return
            logger.warning(f"Retrying Uplift TTS request — attempt {attempt + 1}/{self._MAX_ATTEMPTS}")
