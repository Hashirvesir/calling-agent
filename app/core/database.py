"""Supabase async client — singleton + all CRUD helpers.

All helpers catch exceptions and log them so a DB hiccup never
crashes the voice pipeline. Every function is safe to call from
any async context.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from app.core.config import settings

_client = None
_client_lock = asyncio.Lock()
_STORAGE_BUCKET = "recordings"


async def init_db():
    """Initialize the shared Supabase async client. Call once at startup."""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        try:
            from supabase import create_async_client
            _client = await create_async_client(
                settings.supabase_url,
                settings.supabase_service_role_key,
            )
            logger.info("Supabase async client initialized.")
        except Exception as exc:
            logger.error(f"Supabase init failed: {exc}")
    return _client


async def _db():
    if _client is None:
        await init_db()
    return _client


# =============================================================================
# USER SETTINGS
# =============================================================================

async def get_user_by_webhook_token(token: str) -> Optional[dict]:
    """Look up user_settings by webhook_token. Returns full row including user_id and Telnyx creds."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("user_settings")
            .select("user_id, telnyx_api_key, telnyx_webhook_public_key, webhook_token")
            .eq("webhook_token", token)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_user_by_webhook_token: {exc}")
        return None


async def get_user_settings(user_id: str) -> Optional[dict]:
    """Return user_settings row for a given user_id."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("user_settings")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_user_settings: {exc}")
        return None


async def ensure_user_settings(user_id: str) -> Optional[dict]:
    """Return the user's settings row, creating an empty one (with a generated
    webhook_token) if it doesn't exist yet. Every authenticated user thus has a
    stable webhook URL even before entering their Telnyx credentials."""
    existing = await get_user_settings(user_id)
    if existing:
        return existing
    db = await _db()
    if not db:
        return None
    try:
        res = await db.table("user_settings").insert({"user_id": user_id}).execute()
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB ensure_user_settings: {exc}")
        # Race: another request may have created it — fall back to a read.
        return await get_user_settings(user_id)


async def save_user_settings(
    user_id: str,
    telnyx_api_key: str,
    telnyx_webhook_public_key: str,
) -> Optional[dict]:
    """Upsert Telnyx credentials for a user. Creates the row if it doesn't exist."""
    db = await _db()
    if not db:
        return None
    try:
        res = await db.table("user_settings").upsert(
            {
                "user_id": user_id,
                "telnyx_api_key": telnyx_api_key,
                "telnyx_webhook_public_key": telnyx_webhook_public_key,
            },
            on_conflict="user_id",
        ).execute()
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB save_user_settings: {exc}")
        return None


async def save_user_llm_config(user_id: str, provider: str, model: str) -> Optional[dict]:
    """Persist this user's chosen LLM provider/model, creating their
    user_settings row first if this is their very first save of anything."""
    await ensure_user_settings(user_id)
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("user_settings")
            .update({"llm_provider": provider, "llm_model": model})
            .eq("user_id", user_id)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB save_user_llm_config: {exc}")
        return None


async def save_user_stt_config(user_id: str, provider: str, model: Optional[str] = None) -> Optional[dict]:
    """Persist this user's chosen STT provider (+ model, for providers with
    more than one — see save_user_llm_config). model=None leaves stt_model
    untouched (Groq/Deepgram each use one fixed model, never send one)."""
    await ensure_user_settings(user_id)
    db = await _db()
    if not db:
        return None
    try:
        fields: dict = {"stt_provider": provider}
        if model is not None:
            fields["stt_model"] = model
        res = (
            await db.table("user_settings")
            .update(fields)
            .eq("user_id", user_id)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB save_user_stt_config: {exc}")
        return None


async def save_user_tts_config(user_id: str, provider: str, model: str) -> Optional[dict]:
    """Persist this user's chosen TTS provider/model — see save_user_llm_config."""
    await ensure_user_settings(user_id)
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("user_settings")
            .update({"tts_provider": provider, "tts_model": model})
            .eq("user_id", user_id)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB save_user_tts_config: {exc}")
        return None


async def save_user_pipeline_config(user_id: str, mode: str, voice: str) -> Optional[dict]:
    """Persist this user's chosen voice pipeline mode (cascaded vs. OpenAI
    Realtime) and realtime voice — see save_user_llm_config."""
    await ensure_user_settings(user_id)
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("user_settings")
            .update({"voice_pipeline_mode": mode, "realtime_voice": voice})
            .eq("user_id", user_id)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB save_user_pipeline_config: {exc}")
        return None


# =============================================================================
# SCRIPTS
# =============================================================================

async def get_all_scripts(user_id: str) -> list[dict]:
    db = await _db()
    if not db:
        return []
    try:
        res = (
            await db.table("scripts")
            .select("*")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error(f"DB get_all_scripts: {exc}")
        return []


async def get_script_by_id(script_id: str, user_id: str) -> Optional[dict]:
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("scripts")
            .select("*")
            .eq("id", script_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_script_by_id: {exc}")
        return None


async def create_script(
    name: str,
    content: str,
    user_id: str,
    language: str = "ur",
    extraction_fields: list = [],
) -> Optional[dict]:
    db = await _db()
    if not db:
        return None
    try:
        res = await db.table("scripts").insert({
            "name": name,
            "content": content,
            "language": language,
            "extraction_fields": extraction_fields,
            "user_id": user_id,
        }).execute()
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB create_script: {exc}")
        return None


async def update_script(script_id: str, user_id: str, **fields) -> Optional[dict]:
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("scripts")
            .update(fields)
            .eq("id", script_id)
            .eq("user_id", user_id)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB update_script: {exc}")
        return None


async def delete_script(script_id: str, user_id: str) -> bool:
    db = await _db()
    if not db:
        return False
    try:
        await db.table("agents").update({"script_id": None}).eq("script_id", script_id).eq("user_id", user_id).execute()
        await db.table("scripts").delete().eq("id", script_id).eq("user_id", user_id).execute()
        return True
    except Exception as exc:
        logger.error(f"DB delete_script: {exc}")
        return False


# =============================================================================
# AGENTS
# =============================================================================

async def get_all_agents(user_id: str) -> list[dict]:
    """Every agent owned by this user, active or not — the dashboard list needs
    to show inactive agents too (with an Inactive badge) so they stay
    manageable/reactivatable. Live-call routing uses get_agent_by_number
    instead, which does filter to active agents."""
    db = await _db()
    if not db:
        return []
    try:
        res = (
            await db.table("agents")
            .select("*, scripts(id, name, content, language)")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error(f"DB get_all_agents: {exc}")
        return []


async def get_agent_by_id(agent_id: str, user_id: str) -> Optional[dict]:
    """Fetch by id regardless of active status — used by the edit page and by
    PATCH's own re-fetch, so deactivating an agent must not make it 404."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("agents")
            .select("*, scripts(id, name, content, language, extraction_fields)")
            .eq("id", agent_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_agent_by_id: {exc}")
        return None


async def get_agent_by_number(telnyx_number: str, user_id: str) -> Optional[dict]:
    """Return active agent (with nested script) matching the Telnyx number for a specific user."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("agents")
            .select("*, scripts(id, name, content, language, extraction_fields)")
            .eq("telnyx_number", telnyx_number)
            .eq("user_id", user_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_agent_by_number({telnyx_number}): {exc}")
        return None


async def get_agent_by_telnyx_number_any_user(telnyx_number: str) -> Optional[dict]:
    """Return active agent matching telnyx_number across any user (service-role query)."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("agents")
            .select("*, scripts(id, name, content, language, extraction_fields)")
            .eq("telnyx_number", telnyx_number)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows:
            return rows[0]
        # Fallback to any active agent if specific number not assigned
        res_fallback = (
            await db.table("agents")
            .select("*, scripts(id, name, content, language, extraction_fields)")
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows_fallback = res_fallback.data or []
        return rows_fallback[0] if rows_fallback else None
    except Exception as exc:
        logger.error(f"DB get_agent_by_telnyx_number_any_user({telnyx_number}): {exc}")
        return None


async def get_all_active_agents_with_scripts() -> list[dict]:
    """All active agents across every user, with their nested script — used to
    prewarm RAG at startup so the first call never builds embeddings mid-call.
    Uses the service-role client, so it bypasses RLS intentionally (server-only)."""
    db = await _db()
    if not db:
        return []
    try:
        res = (
            await db.table("agents")
            .select(
                "id, user_id, name, greeting_text, default_language, voice_urdu, voice_english, "
                "tts_provider, tts_model, "
                "scripts(id, name, content, language, extraction_fields)"
            )
            .eq("is_active", True)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error(f"DB get_all_active_agents_with_scripts: {exc}")
        return []


async def get_call_agent_and_script(call_control_id: str) -> Optional[dict]:
    """Single query to get call_id, agent_name, and extraction_fields for auto-extraction."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("calls")
            .select("id, agents(name, scripts(extraction_fields))")
            .eq("call_control_id", call_control_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        call = rows[0]
        agent = call.get("agents") or {}
        if isinstance(agent, list):
            agent = agent[0] if agent else {}
        script = agent.get("scripts") or {}
        if isinstance(script, list):
            script = script[0] if script else {}
        return {
            "call_id": call.get("id"),
            "agent_name": agent.get("name", "unknown_agent"),
            "extraction_fields": script.get("extraction_fields") or [],
        }
    except Exception as exc:
        logger.error(f"DB get_call_agent_and_script: {exc}")
        return None


async def create_agent(
    name: str,
    telnyx_number: str,
    user_id: str,
    script_id: Optional[str] = None,
    telnyx_app_id: Optional[str] = None,
    system_prompt_override: Optional[str] = None,
    voice_urdu: str = "v_8eelc901",
    voice_english: str = "v_8eelc901",
    default_language: str = "ur",
    greeting_text: Optional[str] = None,
) -> Optional[dict]:
    db = await _db()
    if not db:
        return None
    try:
        res = await db.table("agents").insert({
            "name": name,
            "telnyx_number": telnyx_number,
            "script_id": script_id,
            "telnyx_app_id": telnyx_app_id,
            "system_prompt_override": system_prompt_override,
            "voice_urdu": voice_urdu,
            "voice_english": voice_english,
            "default_language": default_language,
            "greeting_text": greeting_text,
            "user_id": user_id,
        }).execute()
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB create_agent: {exc}")
        return None


async def update_agent(agent_id: str, user_id: str, **fields) -> Optional[dict]:
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("agents")
            .update(fields)
            .eq("id", agent_id)
            .eq("user_id", user_id)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception as exc:
        logger.error(f"DB update_agent: {exc}")
        return None


async def delete_agent(agent_id: str, user_id: str) -> bool:
    db = await _db()
    if not db:
        return False
    try:
        await db.table("calls").update({"agent_id": None}).eq("agent_id", agent_id).execute()
        await db.table("agents").delete().eq("id", agent_id).eq("user_id", user_id).execute()
        return True
    except Exception as exc:
        logger.error(f"DB delete_agent: {exc}")
        return False


# =============================================================================
# CALLS
# =============================================================================

async def create_call(
    call_control_id: Optional[str],
    direction: str,
    from_number: Optional[str],
    to_number: Optional[str],
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """Insert a new call. Returns internal UUID or None on failure."""
    db = await _db()
    if not db:
        return None
    try:
        res = await db.table("calls").insert({
            "call_control_id": call_control_id,
            "agent_id": agent_id,
            "direction": direction,
            "from_number": from_number,
            "to_number": to_number,
            "status": "initiated",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
        }).execute()
        call_id = ((res.data or [{}])[0]).get("id")
        logger.info(f"DB: call created id={call_id} ccid={str(call_control_id or '')[:14]}")
        return call_id
    except Exception as exc:
        logger.error(f"DB create_call: {exc}")
        return None


async def update_call(call_control_id: str, **fields) -> None:
    db = await _db()
    if not db:
        return
    try:
        await (
            db.table("calls")
            .update(fields)
            .eq("call_control_id", call_control_id)
            .execute()
        )
    except Exception as exc:
        logger.error(f"DB update_call: {exc}")


async def end_call(call_control_id: str, final_status: str = "ended") -> None:
    """Mark call ended, calculate and save duration_seconds from started_at."""
    db = await _db()
    if not db:
        return
    try:
        res = (
            await db.table("calls")
            .select("started_at, status")
            .eq("call_control_id", call_control_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows and rows[0].get("status") in ("ended", "stream_failed"):
            logger.debug(f"DB: call already ended ccid={call_control_id[:14]}, skipping")
            return
        ended_at = datetime.now(timezone.utc).isoformat()
        fields: dict = {"status": final_status, "ended_at": ended_at}

        if rows and rows[0].get("started_at"):
            try:
                start = datetime.fromisoformat(rows[0]["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                fields["duration_seconds"] = max(0, int((end - start).total_seconds()))
            except Exception:
                pass

        await (
            db.table("calls")
            .update(fields)
            .eq("call_control_id", call_control_id)
            .execute()
        )
        logger.info(f"DB: call ended ccid={call_control_id[:14]} duration={fields.get('duration_seconds')}s")
    except Exception as exc:
        logger.error(f"DB end_call: {exc}")


async def get_call_id_by_ccid(call_control_id: str) -> Optional[str]:
    """Return internal UUID for a given call_control_id."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("calls")
            .select("id")
            .eq("call_control_id", call_control_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0].get("id") if rows else None
    except Exception as exc:
        logger.error(f"DB get_call_id_by_ccid: {exc}")
        return None


async def get_call_status_by_ccid(call_control_id: str) -> Optional[dict]:
    """Return status and duration for a given call_control_id."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("calls")
            .select("id, status, duration_seconds, started_at, ended_at")
            .eq("call_control_id", call_control_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_call_status_by_ccid: {exc}")
        return None


async def save_call_feedback(
    rating: int,
    comment: Optional[str] = None,
    call_control_id: Optional[str] = None,
    phone_number: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> bool:
    """Save user feedback for a call into Supabase."""
    db = await _db()
    if not db:
        return False
    
    call_id = None
    if call_control_id:
        call_id = await get_call_id_by_ccid(call_control_id)
        
    saved = False
    try:
        res = await db.table("call_feedback").insert({
            "call_id": call_id,
            "call_control_id": call_control_id,
            "phone_number": phone_number,
            "rating": rating,
            "comment": comment or "",
            "tags": tags or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        if res.data:
            saved = True
            logger.info(f"DB: Feedback saved in call_feedback table for ccid={str(call_control_id or '')[:14]}")
    except Exception as exc:
        logger.debug(f"call_feedback table insert notice: {exc}")

    try:
        await db.table("call_events").insert({
            "call_control_id": call_control_id,
            "call_id": call_id,
            "event_type": "call_feedback",
            "to_number": phone_number,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "raw_payload": {
                "rating": rating,
                "comment": comment,
                "tags": tags,
                "phone_number": phone_number,
            },
        }).execute()
        saved = True
        logger.info(f"DB: Feedback saved in call_events for phone={phone_number} (rating={rating})")
    except Exception as exc:
        logger.error(f"DB save_call_feedback into call_events failed: {exc}")
        
    return saved


async def cleanup_stuck_calls(max_age_minutes: int = 60) -> int:
    """Mark calls stuck in 'in_progress'/'initiated' as ended if older than max_age_minutes."""
    db = await _db()
    if not db:
        return 0
    try:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        res = (
            await db.table("calls")
            .select("call_control_id, started_at")
            .in_("status", ["in_progress", "initiated"])
            .lt("started_at", cutoff)
            .execute()
        )
        stuck = res.data or []
        count = 0
        for row in stuck:
            ccid = row.get("call_control_id")
            if not ccid:
                continue
            ended_at = datetime.now(timezone.utc).isoformat()
            fields: dict = {"status": "ended", "ended_at": ended_at}
            if row.get("started_at"):
                try:
                    start = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                    fields["duration_seconds"] = max(0, int((end - start).total_seconds()))
                except Exception:
                    pass
            await db.table("calls").update(fields).eq("call_control_id", ccid).execute()
            count += 1
        if count:
            logger.info(f"DB: cleaned up {count} stuck call(s) older than {max_age_minutes}m")
        return count
    except Exception as exc:
        logger.error(f"DB cleanup_stuck_calls: {exc}")
        return 0


async def delete_call(call_id: str, user_id: str) -> bool:
    db = await _db()
    if not db:
        return False
    try:
        check = await db.table("calls").select("id").eq("id", call_id).eq("user_id", user_id).limit(1).execute()
        if not (check.data or []):
            return False
        await db.table("transcript_turns").delete().eq("call_id", call_id).execute()
        await db.table("extracted_data").delete().eq("call_id", call_id).execute()
        await db.table("calls").delete().eq("id", call_id).eq("user_id", user_id).execute()
        return True
    except Exception as exc:
        logger.error(f"DB delete_call: {exc}")
        return False


async def check_call_owner(call_id: str, user_id: str) -> bool:
    """Return True only if the call exists and belongs to user_id."""
    db = await _db()
    if not db:
        return False
    try:
        res = await db.table("calls").select("id").eq("id", call_id).eq("user_id", user_id).limit(1).execute()
        return bool(res.data)
    except Exception as exc:
        logger.error(f"DB check_call_owner: {exc}")
        return False


async def get_calls_list(user_id: str, limit: int = 50) -> list[dict]:
    db = await _db()
    if not db:
        return []
    try:
        res = (
            await db.table("calls")
            .select("*, agents(id, name, telnyx_number)")
            .eq("user_id", user_id)
            .order("started_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error(f"DB get_calls_list: {exc}")
        return []


async def get_call_by_id(call_id: str, user_id: str) -> Optional[dict]:
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("calls")
            .select("*, agents(id, name, telnyx_number)")
            .eq("id", call_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_call_by_id: {exc}")
        return None


# =============================================================================
# CALL EVENTS
# =============================================================================

async def log_event(
    event_type: str,
    call_control_id: Optional[str],
    direction: Optional[str] = None,
    from_number: Optional[str] = None,
    to_number: Optional[str] = None,
    call_leg_id: Optional[str] = None,
    occurred_at: Optional[str] = None,
    raw_payload: Optional[dict] = None,
) -> None:
    """Insert a raw Telnyx webhook event."""
    db = await _db()
    if not db:
        return
    call_id = await get_call_id_by_ccid(call_control_id) if call_control_id else None
    try:
        await db.table("call_events").insert({
            "call_control_id": call_control_id,
            "call_id": call_id,
            "event_type": event_type,
            "direction": direction,
            "from_number": from_number,
            "to_number": to_number,
            "call_leg_id": call_leg_id,
            "occurred_at": occurred_at,
            "raw_payload": raw_payload or {},
        }).execute()
    except Exception as exc:
        logger.error(f"DB log_event({event_type}): {exc}")


# =============================================================================
# TRANSCRIPT TURNS
# =============================================================================

async def log_turn(
    call_id: str,
    speaker: str,
    text: str,
    turn_index: int,
    timestamp_in_call: Optional[str] = None,
) -> None:
    """Insert one USER or BOT turn."""
    db = await _db()
    if not db:
        return
    try:
        await db.table("transcript_turns").insert({
            "call_id": call_id,
            "speaker": speaker,
            "text": text,
            "turn_index": turn_index,
            "timestamp_in_call": timestamp_in_call,
        }).execute()
    except Exception as exc:
        logger.error(f"DB log_turn({speaker}): {exc}")


async def increment_turn_count(call_control_id: str) -> None:
    """Atomically increment turn_count on the call via stored procedure."""
    db = await _db()
    if not db:
        return
    try:
        await db.rpc(
            "increment_call_turn_count",
            {"p_call_control_id": call_control_id},
        ).execute()
    except Exception as exc:
        logger.error(f"DB increment_turn_count: {exc}")


async def get_turns_by_call_id(call_id: str, user_id: Optional[str] = None) -> list[dict]:
    db = await _db()
    if not db:
        return []
    try:
        if user_id is not None:
            check = await db.table("calls").select("id").eq("id", call_id).eq("user_id", user_id).limit(1).execute()
            if not (check.data or []):
                return []
        res = (
            await db.table("transcript_turns")
            .select("*")
            .eq("call_id", call_id)
            .order("turn_index")
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error(f"DB get_turns_by_call_id: {exc}")
        return []


async def save_call_metrics(call_id: str, **fields) -> None:
    """Upsert the aggregated latency/token/character metrics for one call."""
    db = await _db()
    if not db:
        return
    try:
        await (
            db.table("call_metrics")
            .upsert({**fields, "call_id": call_id}, on_conflict="call_id")
            .execute()
        )
    except Exception as exc:
        logger.error(f"DB save_call_metrics: {exc}")


async def get_call_metrics(call_id: str, user_id: str) -> Optional[dict]:
    if not await check_call_owner(call_id, user_id):
        return None
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("call_metrics")
            .select("*")
            .eq("call_id", call_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_call_metrics: {exc}")
        return None


async def get_call_metrics_bulk(call_ids: list[str]) -> dict[str, dict]:
    """Fetch call_metrics rows for many calls in one query, keyed by call_id.

    No user_id ownership check here — callers must pre-filter call_ids to ones
    they've already verified belong to the requesting user (e.g. via get_calls_list).
    """
    if not call_ids:
        return {}
    db = await _db()
    if not db:
        return {}
    try:
        res = (
            await db.table("call_metrics")
            .select("*")
            .in_("call_id", call_ids)
            .execute()
        )
        return {row["call_id"]: row for row in (res.data or [])}
    except Exception as exc:
        logger.error(f"DB get_call_metrics_bulk: {exc}")
        return {}


# =============================================================================
# RECORDING STORAGE
# =============================================================================

# =============================================================================
# EXTRACTED DATA
# =============================================================================

async def get_extracted_data_by_agent(agent_id: str, user_id: str) -> list[dict]:
    """Return all calls for an agent with their extracted data (flattened)."""
    db = await _db()
    if not db:
        return []
    try:
        res = (
            await db.table("calls")
            .select(
                "id, from_number, to_number, direction, started_at, duration_seconds,"
                " extracted_data(extracted_data, missing_fields, confidence, agent_name)"
            )
            .eq("agent_id", agent_id)
            .eq("user_id", user_id)
            .order("started_at", desc=True)
            .execute()
        )
        rows = res.data or []
        result = []
        for call in rows:
            ed = call.get("extracted_data")
            # Supabase returns nested table as a dict or None when no matching row
            if isinstance(ed, list):
                ed = ed[0] if ed else None
            phone = (
                call.get("from_number") if call.get("direction") == "inbound"
                else call.get("to_number")
            )
            row: dict = {
                "call_id": call["id"],
                "phone": phone or "—",
                "started_at": call.get("started_at"),
                "duration_seconds": call.get("duration_seconds"),
                "confidence": ed.get("confidence") if ed else None,
                "missing_count": len(ed.get("missing_fields") or []) if ed else None,
            }
            if ed:
                row.update(ed.get("extracted_data") or {})
            result.append(row)
        return result
    except Exception as exc:
        logger.error(f"DB get_extracted_data_by_agent: {exc}")
        return []


async def save_extracted_data(
    call_id: str,
    agent_name: str,
    extracted: dict,
    missing_fields: list,
    confidence: str,
) -> None:
    """Upsert extraction result for a call (one row per call)."""
    db = await _db()
    if not db:
        return
    try:
        await db.table("extracted_data").upsert({
            "call_id": call_id,
            "agent_name": agent_name,
            "extracted_data": extracted,
            "missing_fields": missing_fields,
            "confidence": confidence,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="call_id").execute()
        logger.info(f"DB: extracted_data saved for call_id={call_id} confidence={confidence}")
    except Exception as exc:
        logger.error(f"DB save_extracted_data: {exc}")


async def get_caller_history(phone_number: str, agent_id: str | None = None, limit: int = 5) -> list[dict]:
    """Return recent ended calls + extracted data for a phone number (inbound or outbound).

    If agent_id is given, only returns calls handled by that specific agent.
    """
    db = await _db()
    if not db:
        return []

    from app.core.phone import phone_variants

    variants = phone_variants(phone_number)
    if not variants:
        return []
    or_clauses = []
    for v in variants:
        or_clauses.append(f"from_number.eq.{v}")
        or_clauses.append(f"to_number.eq.{v}")
    or_filter = ",".join(or_clauses)

    try:
        query = (
            db.table("calls")
            .select("id, direction, from_number, to_number, started_at, extracted_data(extracted_data, confidence)")
            .or_(or_filter)
            .eq("status", "ended")
        )
        if agent_id:
            query = query.eq("agent_id", agent_id)
        res = (
            await query
            .order("started_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        result = []
        for row in rows:
            ed = row.get("extracted_data")
            if isinstance(ed, list):
                ed = ed[0] if ed else None
            result.append({
                "started_at": row.get("started_at"),
                "direction": row.get("direction"),
                "extracted_data": ed.get("extracted_data") if ed else None,
                "confidence": ed.get("confidence") if ed else None,
            })
        return result
    except Exception as exc:
        logger.error(f"DB get_caller_history({phone_number}): {exc}")
        return []


async def search_caller_records(phone_number: str, agent_id: str | None = None, limit: int = 20) -> list[dict]:
    """Return every past ended call for a phone number with its extracted data.

    Matches the number in any stored format (E.164, local 0-prefixed, bare
    national) via phone_variants, so a lookup never misses on formatting.
    Returns full extracted_data per call so the caller can reliably ask about
    appointments or anything else previously captured.
    """
    db = await _db()
    if not db:
        return []

    from app.core.phone import phone_variants

    variants = phone_variants(phone_number)
    if not variants:
        return []

    # Build an OR filter matching from_number OR to_number against every variant.
    or_clauses = []
    for v in variants:
        or_clauses.append(f"from_number.eq.{v}")
        or_clauses.append(f"to_number.eq.{v}")
    or_filter = ",".join(or_clauses)

    try:
        query = (
            db.table("calls")
            .select("id, started_at, from_number, to_number, extracted_data(extracted_data, confidence)")
            .or_(or_filter)
            .eq("status", "ended")
        )
        if agent_id:
            query = query.eq("agent_id", agent_id)
        res = await query.order("started_at", desc=True).limit(limit).execute()
        rows = res.data or []
        records = []
        for row in rows:
            ed = row.get("extracted_data")
            if isinstance(ed, list):
                ed = ed[0] if ed else None
            data = (ed.get("extracted_data") or {}) if ed else {}
            # Only include calls that actually captured something
            non_empty = {k: v for k, v in data.items() if v not in (None, "", [])}
            if not non_empty:
                continue
            records.append({
                "call_date": (row.get("started_at") or "")[:10],
                "confidence": (ed.get("confidence") if ed else None),
                "data": non_empty,
            })
        return records
    except Exception as exc:
        logger.error(f"DB search_caller_records({phone_number}): {exc}")
        return []


async def get_extracted_data(call_id: str) -> Optional[dict]:
    """Return the extraction result row for a call, or None."""
    db = await _db()
    if not db:
        return None
    try:
        res = (
            await db.table("extracted_data")
            .select("*")
            .eq("call_id", call_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error(f"DB get_extracted_data: {exc}")
        return None


async def upload_recording(call_control_id: str, mp3_bytes: bytes) -> Optional[str]:
    """Upload MP3 to Supabase Storage. Returns storage path or None."""
    db = await _db()
    if not db:
        return None
    safe = call_control_id.replace(":", "_").replace("/", "_")
    storage_path = f"rec_{safe}.mp3"
    try:
        await db.storage.from_(_STORAGE_BUCKET).upload(
            storage_path,
            mp3_bytes,
            {"content-type": "audio/mpeg", "x-upsert": "true"},
        )
        logger.info(f"DB: recording uploaded → {storage_path}")
        return storage_path
    except Exception as exc:
        logger.error(f"DB upload_recording: {exc}")
        return None


async def download_recording(storage_path: str) -> Optional[bytes]:
    """Download a recording from Supabase Storage and return raw bytes."""
    db = await _db()
    if not db:
        return None
    try:
        data = await db.storage.from_(_STORAGE_BUCKET).download(storage_path)
        return bytes(data) if data else None
    except Exception as exc:
        logger.error(f"DB download_recording({storage_path}): {exc}")
        return None


async def get_recording_signed_url(storage_path: str, expires_in: int = 3600) -> Optional[str]:
    """Generate a signed URL for a recording in Supabase Storage. Valid for `expires_in` seconds."""
    db = await _db()
    if not db:
        return None
    try:
        result = await db.storage.from_(_STORAGE_BUCKET).create_signed_url(storage_path, expires_in)
        if isinstance(result, dict):
            return result.get("signedURL") or result.get("signedUrl")
        # Some SDK versions return an object with a signed_url attribute
        return getattr(result, "signed_url", None) or getattr(result, "signedURL", None)
    except Exception as exc:
        logger.error(f"DB get_recording_signed_url: {exc}")
        return None


async def delete_extracted_data(call_id: str, user_id: str) -> bool:
    """Delete the extracted_data row for a call. Verifies call ownership first."""
    db = await _db()
    if not db:
        return False
    try:
        check = await db.table("calls").select("id").eq("id", call_id).eq("user_id", user_id).limit(1).execute()
        if not (check.data or []):
            return False
        await db.table("extracted_data").delete().eq("call_id", call_id).execute()
        return True
    except Exception as exc:
        logger.error(f"DB delete_extracted_data: {exc}")
        return False


async def update_extracted_data_field(call_id: str, field: str, value: Any, user_id: str) -> bool:
    """Update a single key inside the extracted_data JSONB column.

    Verifies call ownership first. When the extraction row already exists (the
    normal grid-edit case) we UPDATE only the JSONB column — an upsert would try
    to INSERT a row with a null ``agent_name``/``confidence`` and hit the NOT NULL
    constraints. If no row exists yet we INSERT a minimal valid one.
    """
    db = await _db()
    if not db:
        return False
    try:
        check = await db.table("calls").select("id").eq("id", call_id).eq("user_id", user_id).limit(1).execute()
        if not (check.data or []):
            return False

        res = await db.table("extracted_data").select("extracted_data").eq("call_id", call_id).execute()
        if res.data:
            current: dict = res.data[0].get("extracted_data") or {}
            current[field] = value
            await (
                db.table("extracted_data")
                .update({"extracted_data": current})
                .eq("call_id", call_id)
                .execute()
            )
        else:
            # No extraction row yet — create one. agent_name + confidence are NOT NULL.
            agent_name = "manual_entry"
            call_row = (
                await db.table("calls")
                .select("agents(name)")
                .eq("id", call_id)
                .limit(1)
                .execute()
            )
            if call_row.data:
                ag = call_row.data[0].get("agents")
                if isinstance(ag, list):
                    ag = ag[0] if ag else None
                if ag and ag.get("name"):
                    agent_name = ag["name"]
            await db.table("extracted_data").insert({
                "call_id": call_id,
                "agent_name": agent_name,
                "extracted_data": {field: value},
                "missing_fields": [],
                "confidence": "low",
            }).execute()
        return True
    except Exception as exc:
        logger.error(f"DB update_extracted_data_field: {exc}")
        return False


# =============================================================================
# AUTH CODES — 6-digit signup/reset codes for app/api/auth_flows.py
#
# Two separate tables (not one with a "purpose" column) — they already existed
# in Supabase from an earlier, since-lost backend attempt at this same feature
# (found via a Postgres "did you mean" hint while wiring this up fresh); reused
# as-is rather than creating a redundant parallel table. Schema for both:
# id, email, code_hash, expires_at, attempts, used, created_at.
# =============================================================================

_CODE_TABLES = {"signup": "email_verification_codes", "reset": "password_reset_codes"}


async def create_auth_code(email: str, purpose: str, code_hash: str, expires_at: datetime) -> Optional[dict]:
    db = await _db()
    try:
        res = await db.table(_CODE_TABLES[purpose]).insert({
            "email": email.lower(),
            "code_hash": code_hash,
            "expires_at": expires_at.isoformat(),
        }).execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.error(f"create_auth_code failed: {exc}")
        return None


async def get_latest_auth_code(email: str, purpose: str) -> Optional[dict]:
    db = await _db()
    try:
        res = (
            await db.table(_CODE_TABLES[purpose])
            .select("*")
            .eq("email", email.lower())
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.error(f"get_latest_auth_code failed: {exc}")
        return None


async def increment_auth_code_attempts(purpose: str, code_id: str, current_attempts: int) -> None:
    db = await _db()
    try:
        await db.table(_CODE_TABLES[purpose]).update({"attempts": current_attempts + 1}).eq("id", code_id).execute()
    except Exception as exc:
        logger.error(f"increment_auth_code_attempts failed: {exc}")


async def consume_auth_code(purpose: str, code_id: str) -> None:
    db = await _db()
    try:
        await db.table(_CODE_TABLES[purpose]).update({"used": True}).eq("id", code_id).execute()
    except Exception as exc:
        logger.error(f"consume_auth_code failed: {exc}")


# =============================================================================
# AUTH ADMIN — Supabase Auth user management for the custom signup/reset flows
# =============================================================================

_ADMIN_USERS_PAGE_SIZE = 200
_ADMIN_USERS_MAX_PAGES = 25  # cap: 5000 users, plenty for this app's scale


async def find_auth_user_by_email(email: str):
    """Paginate admin.list_users looking for a case-insensitive email match.

    supabase-py's list_users has no email filter param, so this scans pages —
    fine at this app's user count, would need revisiting at large scale.
    """
    db = await _db()
    try:
        email_lower = email.strip().lower()
        page = 1
        while page <= _ADMIN_USERS_MAX_PAGES:
            users = await db.auth.admin.list_users(page=page, per_page=_ADMIN_USERS_PAGE_SIZE)
            if not users:
                return None
            for u in users:
                if (u.email or "").lower() == email_lower:
                    return u
            if len(users) < _ADMIN_USERS_PAGE_SIZE:
                return None
            page += 1
        return None
    except Exception as exc:
        logger.error(f"find_auth_user_by_email failed: {exc}")
        return None


async def create_auth_user(email: str, password: str):
    db = await _db()
    try:
        res = await db.auth.admin.create_user({
            "email": email.strip().lower(),
            "password": password,
            "email_confirm": False,
        })
        return res.user
    except Exception as exc:
        logger.error(f"create_auth_user failed: {exc}")
        return None


async def confirm_auth_user(user_id: str) -> bool:
    db = await _db()
    try:
        await db.auth.admin.update_user_by_id(user_id, {"email_confirm": True})
        return True
    except Exception as exc:
        logger.error(f"confirm_auth_user failed: {exc}")
        return False


async def set_auth_user_password(user_id: str, new_password: str) -> bool:
    db = await _db()
    try:
        await db.auth.admin.update_user_by_id(user_id, {"password": new_password})
        return True
    except Exception as exc:
        logger.error(f"set_auth_user_password failed: {exc}")
        return False
