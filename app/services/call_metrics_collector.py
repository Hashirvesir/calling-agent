"""Observer that captures per-call latency, LLM token usage, and TTS character
usage from pipecat's built-in MetricsFrame stream (enabled via
PipelineParams(enable_metrics=True, enable_usage_metrics=True) in bot.py).

Per-turn latency is correlated by pipeline event ordering (STT processing
event opens a turn, LLM TTFB fills it, first TTS TTFB closes it) since
MetricsData carries no turn/message id. This is accurate for the normal
sequential flow but can misattribute a turn during barge-in/interruption —
worst case a turn is silently dropped from the latency list. Call-level
token/character totals are summed independently of turn correlation and are
unaffected by this.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    ProcessingMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed


class CallMetricsCollector(BaseObserver):
    """Accumulates latency/usage metrics for one call and persists a single
    aggregated row on flush() (called at pipeline teardown)."""

    def __init__(self, db_call_id: Optional[str] = None):
        super().__init__()
        self._db_call_id = db_call_id
        self._llm_prompt_tokens = 0
        self._llm_completion_tokens = 0
        self._tts_uplift_chars = 0
        self._tts_elevenlabs_chars = 0
        self._turns: list[dict] = []
        self._current: Optional[dict] = None
        self._turn_counter = 0

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not isinstance(frame, MetricsFrame):
            return

        for md in frame.data:
            proc = md.processor or ""

            if isinstance(md, ProcessingMetricsData) and "STT" in proc:
                self._current = {
                    "turn": self._turn_counter,
                    "stt_ms": round(md.value * 1000),
                    "llm_ms": None,
                    "tts_ms": None,
                }
                self._turn_counter += 1

            elif isinstance(md, TTFBMetricsData) and "LLM" in proc:
                if self._current is not None and self._current["llm_ms"] is None:
                    self._current["llm_ms"] = round(md.value * 1000)

            elif isinstance(md, TTFBMetricsData) and "TTS" in proc:
                if self._current is not None and self._current["tts_ms"] is None:
                    self._current["tts_ms"] = round(md.value * 1000)
                    stt = self._current["stt_ms"] or 0
                    llm = self._current["llm_ms"] or 0
                    tts = self._current["tts_ms"] or 0
                    self._current["total_ms"] = stt + llm + tts
                    self._turns.append(self._current)
                    self._current = None

            elif isinstance(md, LLMUsageMetricsData) and "LLM" in proc:
                self._llm_prompt_tokens += md.value.prompt_tokens
                self._llm_completion_tokens += md.value.completion_tokens

            elif isinstance(md, TTSUsageMetricsData) and "TTS" in proc:
                if "Uplift" in proc:
                    self._tts_uplift_chars += md.value
                elif "ElevenLabs" in proc:
                    self._tts_elevenlabs_chars += md.value

    async def flush(self) -> None:
        """Persist the accumulated metrics as one row. Called at pipeline teardown."""
        if not self._db_call_id:
            return
        from app.core.database import save_call_metrics

        try:
            await save_call_metrics(
                self._db_call_id,
                llm_prompt_tokens=self._llm_prompt_tokens,
                llm_completion_tokens=self._llm_completion_tokens,
                tts_uplift_characters=self._tts_uplift_chars,
                tts_elevenlabs_characters=self._tts_elevenlabs_chars,
                turn_latencies=self._turns,
            )
        except Exception as exc:
            logger.error(f"CallMetricsCollector flush failed: {exc}")
