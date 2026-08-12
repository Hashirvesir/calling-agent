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
        stt_cost += duration_min * settings.cost_groq_whisper_per_minute
        llm_cost += (
            m.get("llm_prompt_tokens", 0) / 1_000_000 * settings.cost_groq_llm_input_per_1m
            + m.get("llm_completion_tokens", 0) / 1_000_000 * settings.cost_groq_llm_output_per_1m
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
            "groq_llm_usd": round(llm_cost, 4),
            "groq_stt_usd": round(stt_cost, 4),
            "tts_uplift_usd": round(uplift_cost, 4),
            "tts_elevenlabs_usd": round(eleven_cost, 4),
            "total_usd": round(total_cost, 4),
        },
    }
