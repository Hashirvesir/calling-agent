"""Conversation logger — writes each user/assistant turn to a text file.

USER turns are logged from LLMContextFrame (the fully-accumulated user message
after the user_aggregator has collected all VAD fragments into one turn).
This prevents the transcript from showing one utterance split across multiple
lines due to aggressive VAD silence detection.

BOT turns are still logged from TTSTextFrame, which is emitted sentence-by-
sentence by the TTS service and is already clean.

DB logging is additive — a DB failure never affects the file log.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from pipecat.frames.frames import LLMContextFrame, TTSTextFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed


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

    async def flush(self) -> None:
        """Wait for all in-flight turn writes to finish.

        Called at pipeline teardown so the final turns reach the DB before the
        post-call extraction reads the transcript.
        """
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
                self._last_user_logged = content
                await self._append("USER", content)
                break

        # --- BOT turns: log each TTS sentence ---
        elif isinstance(frame, TTSTextFrame):
            key = (id(frame), frame.text or "")
            if key in self._seen_tts:
                return
            self._seen_tts.add(key)
            await self._append("BOT", frame.text)
