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

Realtime mode (OpenAIRealtimeLLMService) has no separate STT/TTS stages, so
none of the above STT-opens/TTS-closes logic ever fires for it — every turn
was silently dropped from turn_latencies before this was gated separately on
isinstance(data.source, OpenAIRealtimeLLMService), which records its one
TTFB per turn (time to first reply audio — there's no separate stt_ms/tts_ms
to break out) directly, without waiting on the STT/TTS events that never come.
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
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.openai.live.llm import OpenAILiveLLMService


def _is_stt(proc: str) -> bool:
    return "STT" in proc


def _is_tts(proc: str) -> bool:
    """Whether this processor name is a TTS service.

    The "STT" exclusion is load-bearing, not defensive: every STT service is
    named "…STTService", and "GroqSTTService" contains "TTS" as a substring
    (Groq-S-**TTS**-ervice). A bare `"TTS" in proc` therefore matches every STT
    service too, and since the STT branch below closes a turn on the first TTS
    metric it saw, each turn was closed by the STT's own TTFB before the LLM
    or the real TTS ever reported. Confirmed live: every stored turn had
    llm_ms=None and a tts_ms that was actually the STT's TTFB — the dashboard
    blamed TTS for ~1.2s while the real 11-19s LLM stalls never showed up.
    """
    return "TTS" in proc and "STT" not in proc


def _llm_provider_from(source, proc: str, realtime_provider: Optional[str]) -> str:
    """Identify which LLM actually generated a MetricsFrame's tokens, so cost
    calculation (app/api/calls.py, app/api/billing.py) can price it correctly
    instead of assuming every call is Groq — wrong (and, for Realtime, wildly
    understated) now that a call's LLM could be Groq, Cerebras, Together AI,
    the OpenAI gpt-4o failover (ServiceSwitcher in bot.py), OpenAI Realtime,
    or Grok Voice. Grok Voice reuses OpenAIRealtimeLLMService unmodified (see
    app/services/bot.py's REALTIME_PROVIDERS) — isinstance alone can't tell
    the two speech-to-speech providers apart, so the caller passes which one
    this call actually configured (realtime_provider) for that case."""
    if isinstance(source, OpenAILiveLLMService):
        return "gpt_live"
    if isinstance(source, OpenAIRealtimeLLMService):
        return realtime_provider or "openai_realtime"
    if "Groq" in proc:
        return "groq"
    if "Cerebras" in proc:
        return "cerebras"
    if "Together" in proc:
        return "together"
    if "OpenAI" in proc:
        return "openai"
    return "unknown"


class CallMetricsCollector(BaseObserver):
    """Accumulates latency/usage metrics for one call and persists a single
    aggregated row on flush() (called at pipeline teardown)."""

    def __init__(
        self,
        db_call_id: Optional[str] = None,
        realtime_provider: Optional[str] = None,
        stt_provider: Optional[str] = None,
    ):
        super().__init__()
        self._db_call_id = db_call_id
        # Which speech-to-speech provider (if any) this call was configured
        # for — see _llm_provider_from's docstring for why this can't be
        # derived from the frame alone.
        self._realtime_provider = realtime_provider
        # Which STT provider this call was configured for (Groq/Deepgram/
        # Together — see bot.py's get_stt_config), for the same cost-pricing
        # reason as realtime_provider above. Passed explicitly rather than
        # inferred from the MetricsData processor name: Together AI's STT
        # is OpenAISTTService pointed at a different base_url (see bot.py's
        # _build_stt) — its class name alone can't tell that apart from a
        # hypothetical real OpenAI STT usage.
        self._stt_provider = stt_provider
        self._llm_prompt_tokens = 0
        self._llm_completion_tokens = 0
        self._llm_provider: Optional[str] = None
        self._tts_uplift_chars = 0
        self._tts_elevenlabs_chars = 0
        self._turns: list[dict] = []
        self._current: Optional[dict] = None
        self._turn_counter = 0
        # Frame ids already accounted for — see on_push_frame. One entry per
        # metrics frame in a single call, so this stays small and dies with
        # the collector at pipeline teardown.
        self._seen_metric_frames: set = set()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not isinstance(frame, MetricsFrame):
            return

        # on_push_frame fires once per processor-to-processor hop, so a single
        # MetricsFrame travelling the pipeline is delivered here ~15 times.
        # Counting each delivery inflated everything: one real call produced
        # 139 "turns" for ~8 exchanges, with the duplicates repeatedly
        # reopening a turn (discarding the one in progress) and double-counting
        # every token and TTS character. Bill on frame identity, not hops.
        if frame.id in self._seen_metric_frames:
            return
        self._seen_metric_frames.add(frame.id)

        for md in frame.data:
            proc = md.processor or ""

            if isinstance(md, TTFBMetricsData) and isinstance(
                data.source, (OpenAIRealtimeLLMService, OpenAILiveLLMService)
            ):
                # One TTFB per turn, no separate STT/TTS stages to correlate
                # against — record it as a complete turn immediately instead
                # of routing through the STT-opens/TTS-closes state machine
                # below, which never fires for this mode.
                self._llm_provider = _llm_provider_from(data.source, proc, self._realtime_provider)
                ms = round(md.value * 1000)
                self._turns.append({
                    "turn": self._turn_counter,
                    "stt_ms": None,
                    "llm_ms": ms,
                    "tts_ms": None,
                    "total_ms": ms,
                })
                self._turn_counter += 1
                continue

            if isinstance(md, ProcessingMetricsData) and _is_stt(proc):
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

            elif isinstance(md, TTFBMetricsData) and _is_tts(proc):
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
                self._llm_provider = _llm_provider_from(data.source, proc, self._realtime_provider)

            elif isinstance(md, TTSUsageMetricsData) and _is_tts(proc):
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
                llm_provider=self._llm_provider,
                stt_provider=self._stt_provider,
                tts_uplift_characters=self._tts_uplift_chars,
                tts_elevenlabs_characters=self._tts_elevenlabs_chars,
                turn_latencies=self._turns,
            )
        except Exception as exc:
            logger.error(f"CallMetricsCollector flush failed: {exc}")
