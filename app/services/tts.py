#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Uplift TTS service integration."""

from typing import AsyncGenerator, Optional

import aiohttp
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

    Streams WAV audio (WAV_22050_16) from Uplift's streaming endpoint.
    Voices: Urdu (v_8eelc901 / v_kwmp7zxt / v_yypgzenx / v_30s70t3a);
    English falls back to the Urdu default voice.
    """

    _STREAM_URL = "https://api.upliftai.org/v1/synthesis/text-to-speech/stream"

    # Uplift streaming has no library-level timeout, so a hung connection
    # would otherwise stall a call turn for aiohttp's long default. sock_read
    # aborts if the stream stalls between chunks; total caps the worst case.
    _REQUEST_TIMEOUT = aiohttp.ClientTimeout(connect=5, sock_connect=5, sock_read=10, total=30)
    _MAX_ATTEMPTS = 2

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str = "v_8eelc901",
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
        }

    def can_generate_metrics(self) -> bool:
        return True

    async def set_voice_id(self, voice_id: str) -> None:
        """Switch the active voice at runtime."""
        logger.info(f"Switching Uplift streaming TTS voice to: [{voice_id}]")
        self._uplift_config["voice_id"] = voice_id

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        if len(text) > 2500:
            logger.warning(f"Text too long ({len(text)} chars) — truncating to 2500")
            text = text[:2500]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        logger.debug(f"{self}: Generating TTS [{text}]")

        payload = {
            "text": text,
            "voiceId": self._uplift_config["voice_id"],
            "outputFormat": self._uplift_config["output_format"],
        }

        await self.start_ttfb_metrics()
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
