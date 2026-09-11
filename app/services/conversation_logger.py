"""Conversation logger — writes each user/assistant turn to a text file.

USER turns are logged from LLMContextFrame (the fully-accumulated user message
after the user_aggregator has collected all VAD fragments into one turn).
This prevents the transcript from showing one utterance split across multiple
lines due to aggressive VAD silence detection.

BOT turns are buffered between TTSStartedFrame and TTSStoppedFrame and logged
as ONE line per utterance. TTS services differ in TTSTextFrame granularity —
Uplift emits sentences but ElevenLabs emits every WORD as its own frame, which
used to turn each English bot reply into dozens of one-word transcript_turns
rows (wrecking the dashboard transcript, extraction input, and turn counts).

DB logging is additive — a DB failure never affects the file log.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from pipecat.frames.frames import (
    LLMContextFrame,
    LLMFullResponseEndFrame,
    TranscriptionFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.tts_service import TTSService


def _write_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


class ConversationLogger(BaseObserver):
    """Observer that appends user + assistant turns to a single text file
    and saves each turn to Supabase transcript_turns."""

    def __init__(
        self,
        call_id: Optional[str] = None,
        log_dir: str = "calls",
        db_call_id: Optional[str] = None,
        call_control_id: Optional[str] = None,
    ):
        super().__init__()
        Path(log_dir).mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = f"_{call_id}" if call_id else ""
        self._log_path = Path(log_dir) / f"{stamp}{suffix}.txt"

        # DB identifiers — None means DB logging is skipped
        self._db_call_id = db_call_id
        self._call_control_id = call_control_id
        self._turn_index: int = 0

        # Dedup trackers
        self._last_user_logged: str = ""
        self._seen_tts: set[tuple[int, str]] = set()
        # BOT utterance buffer — TTSTextFrame fragments accumulate here and are
        # flushed as one line when the utterance's TTSStoppedFrame passes.
        self._bot_buffer: list[str] = []

        # In-flight DB write tasks. We keep strong references because a bare
        # asyncio.create_task() can be garbage-collected mid-execution, silently
        # dropping a turn (usually the last one) before extraction reads the DB.
        self._pending_db_tasks: set[asyncio.Task] = set()

        with self._log_path.open("w", encoding="utf-8") as f:
            f.write(f"# Call transcript — started {datetime.now().isoformat()}\n")
            if call_id:
                f.write(f"# Call ID: {call_id}\n")
            f.write("\n")

        logger.info(f"Conversation logger writing to {self._log_path.resolve()}")

    async def _append(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        ts = datetime.now().strftime("%H:%M:%S")

        # File log — offloaded to a thread. This runs once per turn on the
        # shared event loop; a synchronous open/write/close here would briefly
        # block every other concurrent call's pipeline too.
        await asyncio.to_thread(_write_line, self._log_path, f"[{ts}] {speaker}: {text}\n")

        # DB log — non-blocking, but we retain the task reference (and drop it on
        # completion) so it can't be garbage-collected before the write finishes.
        if self._db_call_id:
            idx = self._turn_index
            self._turn_index += 1
            task = asyncio.create_task(self._save_to_db(speaker, text, idx, ts))
            self._pending_db_tasks.add(task)
            task.add_done_callback(self._pending_db_tasks.discard)

    async def _save_to_db(self, speaker: str, text: str, turn_index: int, ts: str) -> None:
        try:
            from app.core.database import log_turn, increment_turn_count
            await log_turn(
                call_id=self._db_call_id,
                speaker=speaker,
                text=text,
                turn_index=turn_index,
                timestamp_in_call=ts,
            )
            if self._call_control_id:
                await increment_turn_count(self._call_control_id)
        except Exception as exc:
            logger.error(f"ConversationLogger DB save failed: {exc}")

    async def _flush_bot_buffer(self) -> None:
        """Write the buffered bot fragments as one BOT line, if any."""
        if self._bot_buffer:
            utterance = " ".join(self._bot_buffer)
            self._bot_buffer = []
            await self._append("BOT", utterance)

    async def flush(self) -> None:
        """Wait for all in-flight turn writes to finish.

        Called at pipeline teardown so the final turns reach the DB before the
        post-call extraction reads the transcript.
        """
        # Drain whatever the bot said last so the transcript keeps the final turn.
        await self._flush_bot_buffer()
        if not self._pending_db_tasks:
            return
        pending = list(self._pending_db_tasks)
        logger.info(f"ConversationLogger flushing {len(pending)} pending DB write(s)…")
        await asyncio.gather(*pending, return_exceptions=True)

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        # --- USER turns: log the last user message from the accumulated context ---
        # LLMContextFrame is emitted by user_aggregator (and again by RAGContextInjector).
        # We only log when the user content actually changes to avoid duplicates.
        if isinstance(frame, LLMContextFrame):
            try:
                messages = frame.context.get_messages()
            except Exception:
                return
            for msg in reversed(messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                if not isinstance(content, str):
                    continue
                # Strip RAG/system injected markers that sometimes appear in content
                content = content.strip()
                if not content:
                    continue
                if content == self._last_user_logged:
                    break  # already logged this turn
                # The bot's previous reply must land before this new user turn,
                # or the transcript order inverts.
                await self._flush_bot_buffer()
                self._last_user_logged = content
                await self._append("USER", content)
                break

        # --- USER turns, Realtime mode: OpenAIRealtimeLLMService has no
        # user_aggregator-produced LLMContextFrame carrying the caller's words
        # the way cascaded mode does — its TranscriptionFrame arrives a
        # different way (pushed upstream by the service itself once OpenAI's
        # own transcription completes). Gated on source type so this can never
        # fire for cascaded-mode STT's TranscriptionFrame, and routed through
        # the same _last_user_logged dedupe as the branch above so it can't
        # double-log even if a context aggregator also re-emits this turn.
        elif isinstance(frame, TranscriptionFrame) and isinstance(data.source, OpenAIRealtimeLLMService):
            content = (frame.text or "").strip()
            if content and content != self._last_user_logged:
                await self._flush_bot_buffer()
                self._last_user_logged = content
                await self._append("USER", content)

        # --- BOT turns: buffer TTS fragments, flush one line per response ---
        elif isinstance(frame, TTSTextFrame):
            key = (id(frame), frame.text or "")
            if key in self._seen_tts:
                return
            self._seen_tts.add(key)
            text = (frame.text or "").strip()
            # Skip punctuation-only fragments ("..", ".") — the LLM sometimes
            # emits bare dots and they were being logged (and spoken) verbatim.
            if text and any(ch.isalnum() for ch in text):
                self._bot_buffer.append(text)

        # End-of-response triggers. TTSStoppedFrame alone is NOT enough —
        # pipecat's TTSService only pushes it when push_stop_frames=True, which
        # UpliftStreamingTTSService doesn't set, so an entire call's bot speech
        # once accumulated into one giant line that flushed at teardown.
        elif isinstance(frame, TTSStoppedFrame):
            await self._flush_bot_buffer()

        # LLMFullResponseEndFrame follows every LLM reply, but only the copy the
        # TTS service forwards is safe to flush on: by then every TTSTextFrame
        # of the response has been pushed. The LLM's own earlier push races the
        # TTS synthesis and would split the line mid-response.
        elif isinstance(frame, LLMFullResponseEndFrame) and isinstance(data.source, TTSService):
            await self._flush_bot_buffer()
