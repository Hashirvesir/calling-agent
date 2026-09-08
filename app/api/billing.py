"""Billing usage — read-only placeholder page data.

No real subscription/payment system exists yet (no plan table, no Stripe).
This surfaces real current-month usage/cost from calls + call_metrics so the
dashboard's Billing page shows honest numbers instead of a fabricated plan.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_calls_list, get_call_metrics_bulk

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/usage")
async def get_usage(user_id: str = Depends(get_current_user)):
    calls = await get_calls_list(user_id, limit=1000)

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _started_at(call: dict) -> datetime | None:
        raw = call.get("started_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    month_calls = [c for c in calls if (_started_at(c) or month_start) >= month_start]
    call_ids = [c["id"] for c in month_calls if c.get("id")]
    metrics_by_call = await get_call_metrics_bulk(call_ids)

    total_minutes = 0.0
    telnyx_cost = telnyx_recording_cost = stt_cost = llm_cost = uplift_cost = eleven_cost = 0.0

    for call in month_calls:
        duration_min = (call.get("duration_seconds") or 0) / 60
        total_minutes += duration_min
        m = metrics_by_call.get(call["id"], {})

        telnyx_cost += duration_min * settings.cost_telnyx_per_minute
        telnyx_recording_cost += duration_min * settings.cost_telnyx_recording_per_minute

        # Which LLM generated this call's tokens — a month can mix Groq/
        # Cerebras/Together/OpenAI-failover/OpenAI-Realtime calls, each priced
        # differently (see CallMetricsCollector._llm_provider and the
        # per-call breakdown in app/api/calls.py). Legacy rows predating
        # migration 011 have no value here; assume Groq, the long-standing
        # default.
        llm_provider = m.get("llm_provider") or "groq"
        prompt_tok = m.get("llm_prompt_tokens", 0)
        completion_tok = m.get("llm_completion_tokens", 0)
        if llm_provider == "openai_realtime":
            llm_cost += (
                prompt_tok / 1_000_000 * settings.cost_openai_realtime_input_per_1m
                + completion_tok / 1_000_000 * settings.cost_openai_realtime_output_per_1m
            )
            # No separate STT fee — bundled into the token cost above.
        elif llm_provider == "grok_voice":
            # Per audio-minute, not per token — see app/api/calls.py's
            # matching branch for why.
            llm_cost += duration_min * settings.cost_grok_voice_per_minute
        else:
            # Which STT transcribed this call — Groq/Deepgram/Together each
            # have different per-minute rates (see the matching branch in
            # app/api/calls.py). Legacy rows predating migration 014 have no
            # value here; assume Groq, the long-standing default.
            stt_provider = m.get("stt_provider") or "groq"
            if stt_provider == "deepgram":
                stt_cost += duration_min * settings.cost_deepgram_stt_per_minute
            elif stt_provider == "together":
                stt_cost += duration_min * settings.cost_together_stt_per_minute
            else:
                stt_cost += duration_min * settings.cost_groq_whisper_per_minute
            if llm_provider == "openai":
                llm_cost += (
                    prompt_tok / 1_000_000 * settings.cost_openai_gpt4o_input_per_1m
                    + completion_tok / 1_000_000 * settings.cost_openai_gpt4o_output_per_1m
                )
            elif llm_provider == "together":
                llm_cost += (
                    prompt_tok / 1_000_000 * settings.cost_together_llm_input_per_1m
                    + completion_tok / 1_000_000 * settings.cost_together_llm_output_per_1m
                )
            else:
                llm_cost += (
                    prompt_tok / 1_000_000 * settings.cost_groq_llm_input_per_1m
                    + completion_tok / 1_000_000 * settings.cost_groq_llm_output_per_1m
                )
        uplift_cost += m.get("tts_uplift_characters", 0) * settings.cost_uplift_per_character
        eleven_cost += m.get("tts_elevenlabs_characters", 0) * settings.cost_elevenlabs_per_character

    total_cost = telnyx_cost + telnyx_recording_cost + stt_cost + llm_cost + uplift_cost + eleven_cost

    return {
        "period_start": month_start.isoformat(),
        "total_calls": len(month_calls),
        "total_minutes": round(total_minutes, 1),
        "cost": {
            "telnyx_usd": round(telnyx_cost, 4),
            "telnyx_recording_usd": round(telnyx_recording_cost, 4),
            "llm_usd": round(llm_cost, 4),
            "stt_usd": round(stt_cost, 4),
            "tts_uplift_usd": round(uplift_cost, 4),
            "tts_elevenlabs_usd": round(eleven_cost, 4),
            "total_usd": round(total_cost, 4),
        },
    }
