"""Telnyx Programmable Voice webhooks + WebSocket media stream.

Multi-tenant routing:
    POST /webhook/{webhook_token}  — per-user webhook URL
    WS   /ws                       — Telnyx media stream → pipecat pipeline
    POST /dial                     — Initiate outbound call (requires JWT)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, unquote

import aiohttp
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from loguru import logger

from pipecat.runner.types import WebSocketRunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.telnyx import TelnyxFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from app.services.bot import bot, run_bot
from app.services.email import trigger_sales_lead_emails
from app.core.auth import get_current_user
from app.core.database import (
    create_call, update_call, end_call, get_call_id_by_ccid,
    get_call_status_by_ccid, save_call_feedback, save_sales_lead,
    log_event, upload_recording, get_agent_by_number,
    get_agent_by_telnyx_number_any_user,
    get_call_agent_and_script, get_caller_history,
    get_user_by_webhook_token, get_user_settings,
)
from app.core.phone import normalize_phone
from app.core.config import settings
from app.core.pipeline_config import REALTIME_PROVIDERS, get_pipeline_config
from app.core.redis_client import get_redis

router = APIRouter(tags=["telnyx"])

TELNYX_API = "https://api.telnyx.com/v2"
_WEBHOOK_TOLERANCE_SECS = 300

# ---------------------------------------------------------------------------
# Webhook signature verification (Ed25519)
# ---------------------------------------------------------------------------

def _verify_webhook_signature(body: bytes, timestamp: str, signature_b64: str, pub_key_b64: str) -> bool:
    if not pub_key_b64:
        logger.warning("No webhook public key — skipping signature verification")
        return True
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > _WEBHOOK_TOLERANCE_SECS:
            logger.warning(f"Webhook timestamp too old: {ts}")
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        pub_bytes = base64.b64decode(pub_key_b64)
        public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        message = f"{timestamp}|".encode() + body
        public_key.verify(base64.b64decode(signature_b64), message)
        return True
    except Exception as exc:
        logger.warning(f"Webhook signature invalid: {exc}")
        return False

# ---------------------------------------------------------------------------
# Media-stream URL signing
#
# /ws has no JWT (Telnyx connects to it, not the browser), so the only thing
# making it safe is that ONLY our own call.answered handler mints stream URLs.
# The HMAC below covers every query param the WS handler trusts — without it,
# anyone who knew a user's UUID could open a WS and run the whole pipeline on
# the platform's LLM/TTS keys (and drive the victim's Telnyx key).
# ---------------------------------------------------------------------------

_WS_SIG_TTL = 300  # seconds — Telnyx connects within moments of streaming_start


def _ws_signature(ccid: str, uid: str, direction: str, from_num: str, to_num: str, exp: int) -> str:
    """HMAC over the raw (unquoted) stream-URL params. The Supabase service
    role key doubles as the signing secret — server-side only, always set."""
    msg = f"{ccid}|{uid}|{direction}|{from_num}|{to_num}|{exp}".encode()
    return hmac.new(settings.supabase_service_role_key.encode(), msg, hashlib.sha256).hexdigest()

# ---------------------------------------------------------------------------
# Persistent aiohttp session
# ---------------------------------------------------------------------------

_telnyx_session: aiohttp.ClientSession | None = None

async def _get_telnyx_session() -> aiohttp.ClientSession:
    global _telnyx_session
    if _telnyx_session is None or _telnyx_session.closed:
        _telnyx_session = aiohttp.ClientSession()
    return _telnyx_session

# ---------------------------------------------------------------------------
# Cross-worker dedup helpers
#
# With a single uvicorn worker, plain in-process sets/dicts are enough. With
# multiple workers (separate OS processes, no shared memory) the same call's
# webhook events and WS connection can land on different workers, so "have I
# already seen this ccid" has to live somewhere all workers can see — Redis.
#
# If REDIS_URL isn't configured, everything below falls back to the original
# in-process set, so single-worker/dev usage needs zero setup.
# ---------------------------------------------------------------------------

async def _claim_once(key: str, ttl: int, fallback: dict[str, float]) -> bool:
    """Atomically claim `key` so only the first caller (across all workers)
    proceeds. Returns True if this call is the first to claim it.

    The in-process fallback maps key → expiry time so its TTL semantics match
    Redis, and stale entries are purged on every claim.
    """
    r = await get_redis()
    if r is not None:
        try:
            return bool(await r.set(key, "1", nx=True, ex=ttl))
        except Exception as exc:
            logger.warning(f"Redis claim failed for {key}, using in-process fallback: {exc}")
    now = time.time()
    for stale in [k for k, exp in fallback.items() if exp <= now]:
        fallback.pop(stale, None)
    if key in fallback:
        return False
    fallback[key] = now + ttl
    if len(fallback) > 10_000:
        # Last-resort cap. Evict the oldest half (dict insertion order is
        # chronological here) — the old clear() wiped every LIVE claim at
        # once, letting duplicate extractions and duplicate WS sessions through.
        logger.warning(f"Claim fallback over capacity ({len(fallback)}) — evicting oldest half")
        for k in list(fallback)[: len(fallback) // 2]:
            fallback.pop(k, None)
    return True


async def _release_once(key: str, fallback: dict[str, float]) -> None:
    r = await get_redis()
    if r is not None:
        try:
            await r.delete(key)
            return
        except Exception as exc:
            logger.warning(f"Redis release failed for {key}: {exc}")
    fallback.pop(key, None)

# ---------------------------------------------------------------------------
# Post-call extraction
# ---------------------------------------------------------------------------

_extraction_done: dict[str, float] = {}  # fallback when Redis is unavailable (key → expiry)
_EXTRACTION_DEDUP_TTL = 3600  # 1h — comfortably longer than any retry window
_EXTRACTION_MAX_ATTEMPTS = 2
_EXTRACTION_RETRY_DELAY = 5.0

async def _trigger_extraction_after_call(call_control_id: str) -> None:
    if not call_control_id:
        return
    claimed = await _claim_once(
        f"extraction:done:{call_control_id}", _EXTRACTION_DEDUP_TTL, _extraction_done
    )
    if not claimed:
        return

    await asyncio.sleep(5)

    try:
        data = await get_call_agent_and_script(call_control_id)
    except Exception as exc:
        logger.error(f"Auto-extraction: could not load call/agent data for {call_control_id[:14]}: {exc}")
        return
    if not data:
        return

    call_id = data.get("call_id")
    agent_name = data.get("agent_name", "unknown_agent")
    extraction_fields = data.get("extraction_fields") or []
    if not call_id or not extraction_fields:
        return

    from app.extraction.models import ExtractionSchema
    from app.extraction.service import get_extraction_service

    schema = ExtractionSchema.from_raw(agent_name, extraction_fields)
    service = get_extraction_service()

    for attempt in range(1, _EXTRACTION_MAX_ATTEMPTS + 1):
        try:
            result = await service.extract(schema=schema, call_id=call_id)
            logger.info(
                f"Auto-extraction done: agent={agent_name} call_id={call_id} "
                f"confidence={result.confidence} missing={result.missing_fields}"
            )
            return
        except Exception as exc:
            logger.warning(
                f"Auto-extraction attempt {attempt}/{_EXTRACTION_MAX_ATTEMPTS} failed for "
                f"{call_control_id[:14]}: {exc}"
            )
            if attempt < _EXTRACTION_MAX_ATTEMPTS:
                await asyncio.sleep(_EXTRACTION_RETRY_DELAY)

    logger.error(
        f"Auto-extraction gave up after {_EXTRACTION_MAX_ATTEMPTS} attempts for "
        f"{call_control_id[:14]} — re-run manually via /api/extraction/test"
    )

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

_active_ws: dict[str, float] = {}  # fallback when Redis is unavailable (key → expiry)
_ACTIVE_WS_TTL = 6 * 3600  # safety net so a killed worker can't wedge a ccid forever

_outbound_registry: dict[str, tuple[str, float]] = {}  # fallback: ccid → (from_number, timestamp)
_OUTBOUND_TTL = 120


async def _register_outbound(ccid: str, from_number: str) -> None:
    r = await get_redis()
    if r is not None:
        try:
            await r.set(f"outbound:{ccid}", from_number, ex=_OUTBOUND_TTL)
            return
        except Exception as exc:
            logger.warning(f"Redis outbound register failed for {ccid}, using in-process fallback: {exc}")
    _outbound_registry[ccid] = (from_number, time.time())


async def _pop_outbound(ccid: str) -> str | None:
    """Return the registered from_number for an outbound ccid, if still fresh, clearing it."""
    r = await get_redis()
    if r is not None:
        try:
            value = await r.get(f"outbound:{ccid}")
            if value is not None:
                await r.delete(f"outbound:{ccid}")
            return value
        except Exception as exc:
            logger.warning(f"Redis outbound lookup failed for {ccid}, using in-process fallback: {exc}")
    entry = _outbound_registry.pop(ccid, None)
    now = time.time()
    stale = [k for k, (_, ts) in _outbound_registry.items() if now - ts > _OUTBOUND_TTL]
    for k in stale:
        _outbound_registry.pop(k, None)
    if not entry:
        return None
    from_number, ts = entry
    if now - ts > _OUTBOUND_TTL:
        return None
    return from_number

# ---------------------------------------------------------------------------
# Telnyx helpers — accept per-user api_key
# ---------------------------------------------------------------------------

async def _telnyx_post(path: str, payload: dict, api_key: str) -> tuple[int, dict | str]:
    url = f"{TELNYX_API}{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    session = await _get_telnyx_session()
    async with session.post(url, json=payload, headers=headers) as resp:
        text = await resp.text()
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = text
        if resp.status >= 300:
            if resp.status == 422:
                logger.debug(f"Telnyx POST {path} 422: {text[:80]}")
            else:
                logger.error(f"Telnyx POST {path} failed [{resp.status}]: {text}")
        else:
            logger.info(f"Telnyx POST {path} ok: {text[:120]}")
        return resp.status, body


async def _telnyx_action(call_control_id: str, action: str, api_key: str, payload: dict | None = None):
    return await _telnyx_post(f"/calls/{call_control_id}/actions/{action}", payload or {}, api_key)


def _telnyx_error_message(status: int, body: dict | str) -> str:
    """Turn a raw Telnyx error response into one clean, actionable sentence.

    Telnyx errors look like:
        {"errors": [{"code": 10010, "detail": "Account is disabled D17 ..."}],
         "telnyx_error": {"error_code": "D17"}}
    We surface a friendly message for the common, user-fixable cases so the
    frontend can show it directly instead of dumping raw JSON.
    """
    detail = ""
    tx_code = ""
    if isinstance(body, dict):
        errs = body.get("errors") or []
        if isinstance(errs, list) and errs:
            first = errs[0] or {}
            detail = (first.get("detail") or first.get("title") or "").strip()
        tx_code = str((body.get("telnyx_error") or {}).get("error_code") or "")
    else:
        detail = str(body)[:300]

    low = detail.lower()
    if tx_code == "D17" or "account is disabled" in low or "blocked" in low:
        return (
            "Your Telnyx account is disabled or blocked (error D17). Add a payment "
            "method/balance and complete Level 2 verification in the Telnyx portal, "
            "then try again."
        )
    if status == 401 or "authenticate" in low or "unauthorized" in low or "invalid api key" in low:
        return "Telnyx rejected the API key. Check your Telnyx API Key in Settings."
    if status == 404:
        return "Telnyx could not find the call resource — check the agent's Telnyx App ID and number."
    if status == 429 or "rate limit" in low:
        return "Telnyx rate limit hit — wait a moment and try again."
    if status == 422:
        return detail or "Telnyx rejected the request (invalid number or connection settings)."
    return detail or f"Telnyx call failed (HTTP {status})."


_RECORDING_MAX_ATTEMPTS = 3
_RECORDING_RETRY_DELAY = 3.0


async def _start_recording(call_control_id: str, api_key: str, delay: float = 2.0) -> None:
    await asyncio.sleep(delay)
    for attempt in range(1, _RECORDING_MAX_ATTEMPTS + 1):
        status, body = await _telnyx_action(
            call_control_id, "record_start", api_key,
            {"format": "mp3", "channels": "single", "play_beep": False},
        )
        if status < 300:
            logger.info(f"Recording started for {call_control_id[:14]}… (attempt {attempt})")
            return
        logger.warning(
            f"record_start failed [{status}] for {call_control_id[:14]}… "
            f"(attempt {attempt}/{_RECORDING_MAX_ATTEMPTS})"
        )
        if attempt < _RECORDING_MAX_ATTEMPTS:
            await asyncio.sleep(_RECORDING_RETRY_DELAY)
    logger.error(f"record_start gave up after {_RECORDING_MAX_ATTEMPTS} attempts for {call_control_id[:14]}… — call will not be recorded")


async def _download_and_store_recording(call_control_id: str, url: str) -> None:
    mp3_bytes: bytes | None = None
    for attempt in range(1, _RECORDING_MAX_ATTEMPTS + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"Recording download failed [{resp.status}] (attempt {attempt}/{_RECORDING_MAX_ATTEMPTS}): {url}"
                        )
                    else:
                        mp3_bytes = await resp.read()
                        break
        except Exception as exc:
            logger.warning(
                f"Recording download error (attempt {attempt}/{_RECORDING_MAX_ATTEMPTS}): {exc}"
            )
        if attempt < _RECORDING_MAX_ATTEMPTS:
            await asyncio.sleep(_RECORDING_RETRY_DELAY)

    if mp3_bytes is None:
        logger.error(f"Recording download gave up after {_RECORDING_MAX_ATTEMPTS} attempts for {call_control_id[:14]}…")
        return

    try:
        storage_path = await upload_recording(call_control_id, mp3_bytes)
        if storage_path:
            await update_call(call_control_id, recording_storage_path=storage_path)
            logger.info(f"Recording stored: {storage_path}")
    except Exception as exc:
        logger.error(f"Recording store error: {exc}")

# ---------------------------------------------------------------------------
# Webhook — per-user token routing
# ---------------------------------------------------------------------------

@router.get("/webhook/{webhook_token}")
@router.get("/webhook")
async def webhook_verify():
    return {"ok": True}


@router.post("/webhook/{webhook_token}")
async def webhook(webhook_token: str, request: Request):
    # Look up user by webhook_token
    try:
        user_row = await get_user_by_webhook_token(webhook_token)
    except Exception as exc:
        logger.error(f"Webhook token lookup failed (DB error): {exc}")
        raise HTTPException(503, "Service temporarily unavailable")
    if not user_row:
        raise HTTPException(403, "Unknown webhook token")

    user_id: str = user_row["user_id"]
    telnyx_api_key: str = user_row.get("telnyx_api_key") or ""
    telnyx_webhook_public_key: str = user_row.get("telnyx_webhook_public_key") or ""

    body = await request.body()
    sig = request.headers.get("telnyx-signature-ed25519", "")
    ts  = request.headers.get("telnyx-timestamp", "")
    if telnyx_webhook_public_key:
        # Key configured → signature is REQUIRED. Verifying only when the
        # headers happened to be present let an attacker bypass the whole
        # check by simply omitting them — the signature exists precisely so a
        # leaked webhook URL alone is not enough.
        if not (sig and ts) or not _verify_webhook_signature(body, ts, sig, telnyx_webhook_public_key):
            raise HTTPException(403, "Invalid webhook signature")
    elif sig and ts:
        logger.warning(
            f"Webhook signed but user {user_id[:8]}… has no public key configured — cannot verify"
        )

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(f"Webhook body not valid JSON (user={user_id[:8]}…): {exc}")
        raise HTTPException(400, "Invalid webhook payload")

    event = data.get("data", {}) or {}
    event_type = event.get("event_type")
    payload = event.get("payload", {})
    call_control_id = payload.get("call_control_id")
    direction = payload.get("direction")
    from_num = payload.get("from")
    to_num = payload.get("to")

    logger.info(f"Telnyx event: {event_type} (user={user_id[:8]}…)")

    asyncio.create_task(log_event(
        event_type=event_type,
        call_control_id=call_control_id,
        direction=direction,
        from_number=from_num,
        to_number=to_num,
        call_leg_id=payload.get("call_leg_id"),
        occurred_at=payload.get("occurred_at"),
        raw_payload=payload,
    ))

    if event_type == "call.initiated":
        if direction == "incoming":
            agent = await get_agent_by_number(to_num, user_id) if to_num else None
            agent_id = agent.get("id") if agent else None
            # Awaited, not fire-and-forget: the WS handler looks this row up
            # moments after the answer — a slow insert used to race it, and the
            # whole call then silently lost transcripts, metrics and extraction.
            await create_call(
                call_control_id=call_control_id,
                direction="inbound",
                from_number=from_num,
                to_number=to_num,
                agent_id=agent_id,
                user_id=user_id,
            )
            try:
                ans_status, ans_body = await _telnyx_action(call_control_id, "answer", telnyx_api_key)
            except Exception as exc:
                logger.error(f"answer action could not reach Telnyx: {exc}")
                ans_status, ans_body = 502, str(exc)
            if ans_status >= 300:
                # Most common here is D17 (account disabled) or a bad API key. The
                # call can never be answered, so close the record instead of
                # leaving it stuck in "initiated".
                logger.error(f"Inbound answer failed [{ans_status}]: {_telnyx_error_message(ans_status, ans_body)}")
                if call_control_id:
                    asyncio.create_task(end_call(call_control_id, final_status="answer_failed"))

    elif event_type == "call.answered":
        if not settings.public_host:
            # No stream can ever start — hang up instead of leaving the caller
            # on an answered-but-silent line with the record stuck in
            # "in_progress" until periodic cleanup.
            logger.error("PUBLIC_HOST not set — cannot start media stream; hanging up")
            if call_control_id:
                asyncio.create_task(_telnyx_action(call_control_id, "hangup", telnyx_api_key))
                asyncio.create_task(end_call(call_control_id, final_status="config_error"))
            return {"ok": False, "error": "PUBLIC_HOST not configured"}
        asyncio.create_task(update_call(call_control_id, status="in_progress"))
        ws_dir = direction or "incoming"
        exp = int(time.time()) + _WS_SIG_TTL
        stream_url = (
            f"wss://{settings.public_host}/ws"
            f"?ccid={call_control_id}"
            f"&uid={user_id}"
            f"&dir={ws_dir}"
            f"&from_num={quote(from_num or '')}"
            f"&to_num={quote(to_num or '')}"
            f"&exp={exp}"
            f"&sig={_ws_signature(call_control_id or '', user_id, ws_dir, from_num or '', to_num or '', exp)}"
        )
        logger.info(f"Starting stream: {stream_url[:80]}…")
        try:
            # Bidirectional "rtp" is required: TelnyxFrameSerializer sends raw PCMU media, and classic streaming only plays base64 MP3.
            strm_status, strm_body = await _telnyx_action(
                call_control_id, "streaming_start", telnyx_api_key,
                {
                    "stream_url": stream_url,
                    "stream_track": "inbound_track",
                    "stream_bidirectional_mode": "rtp",
                    "stream_bidirectional_codec": "PCMU",
                },
            )
        except Exception as exc:
            logger.error(f"streaming_start could not reach Telnyx: {exc}")
            strm_status, strm_body = 502, str(exc)
        if strm_status >= 300:
            logger.error(f"streaming_start failed [{strm_status}]: {_telnyx_error_message(strm_status, strm_body)}")
            if call_control_id:
                asyncio.create_task(end_call(call_control_id, final_status="stream_start_failed"))

    elif event_type == "streaming.failed":
        logger.error(f"streaming.failed: {payload}")
        if call_control_id:
            asyncio.create_task(end_call(call_control_id, final_status="stream_failed"))
            asyncio.create_task(_trigger_extraction_after_call(call_control_id))

    elif event_type == "call.recording.saved":
        rec_urls = payload.get("recording_urls") or {}
        pub_urls = payload.get("public_recording_urls") or {}
        url = rec_urls.get("mp3") or pub_urls.get("mp3")
        if url and call_control_id:
            asyncio.create_task(_download_and_store_recording(call_control_id, url))

    elif event_type == "call.machine.detection.ended":
        result = payload.get("result")
        logger.info(f"Machine detection ended: result={result} ccid={call_control_id}")
        if result in ("machine", "silence") and call_control_id:
            logger.warning(f"Answering machine / IVR detected ({result}) — hanging up ccid={call_control_id[:14]}…")
            asyncio.create_task(_telnyx_action(call_control_id, "hangup", telnyx_api_key))
            asyncio.create_task(end_call(call_control_id, final_status="machine_detected"))

    elif event_type == "call.hangup":
        logger.info(f"Call ended (hangup): {call_control_id}")
        asyncio.create_task(end_call(call_control_id))
        asyncio.create_task(_trigger_extraction_after_call(call_control_id))

    elif event_type == "streaming.stopped":
        logger.info(f"Stream stopped: {call_control_id}")
        asyncio.create_task(end_call(call_control_id))
        asyncio.create_task(_trigger_extraction_after_call(call_control_id))

    return {"ok": True}

# ---------------------------------------------------------------------------
# WebSocket (media stream)
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()

    ccid           = websocket.query_params.get("ccid")
    user_id        = websocket.query_params.get("uid")
    call_direction = websocket.query_params.get("dir", "incoming")
    from_num       = unquote(websocket.query_params.get("from_num", "")) or None
    to_num         = unquote(websocket.query_params.get("to_num",  "")) or None
    stream_sig     = websocket.query_params.get("sig", "")

    # Verify the stream-URL HMAC before touching the DB or starting anything —
    # only our own call.answered handler can mint a valid URL (see _ws_signature).
    try:
        exp = int(websocket.query_params.get("exp", ""))
    except ValueError:
        exp = 0
    expected_sig = _ws_signature(
        ccid or "", user_id or "", call_direction, from_num or "", to_num or "", exp
    )
    if exp < time.time() or not hmac.compare_digest(expected_sig, stream_sig):
        logger.warning(f"WS rejected: bad or expired stream signature (ccid={str(ccid)[:14]}…)")
        await websocket.close(code=1008)
        return

    if ccid:
        logger.info(f"WebSocket: ccid={ccid[:14]}… dir={call_direction} uid={user_id}")
    else:
        logger.warning("WebSocket connected with no ccid in query params")

    is_outbound = call_direction == "outgoing"
    if not is_outbound and ccid:
        reg_from_num = await _pop_outbound(ccid)
        if reg_from_num is not None:
            is_outbound = True
            if not from_num:
                from_num = reg_from_num

    session_key = ccid or f"anon-{time.time()}"
    if not await _claim_once(f"ws:active:{session_key}", _ACTIVE_WS_TTL, _active_ws):
        logger.warning(f"Duplicate WebSocket for {session_key[:14]} — closing")
        await websocket.close(code=1008)
        return

    lookup_number = from_num if is_outbound else to_num

    # Phase 1 — these three Supabase lookups are independent. Running them together
    # costs ~one round-trip instead of three before the greeting can play.
    us_coro     = get_user_settings(user_id) if user_id else asyncio.sleep(0, result=None)
    agent_coro  = (get_agent_by_number(lookup_number, user_id)
                   if (lookup_number and user_id) else asyncio.sleep(0, result=None))
    callid_coro = get_call_id_by_ccid(ccid) if ccid else asyncio.sleep(0, result=None)
    user_row, agent, db_call_id = await asyncio.gather(us_coro, agent_coro, callid_coro)

    if ccid and db_call_id is None:
        # create_call is now awaited in the webhook before answering, but DB
        # read-after-write latency can still race this lookup. One short retry
        # instead of silently dropping the whole call's transcripts/extraction.
        await asyncio.sleep(0.5)
        db_call_id = await get_call_id_by_ccid(ccid)

    telnyx_api_key = (user_row.get("telnyx_api_key") or "") if user_row else ""
    if agent:
        logger.info(f"Agent matched: {agent.get('name')} (number={lookup_number})")

    # Phase 2 — caller history needs the matched agent id, so it follows Phase 1.
    # Fetched as a background task so it overlaps with transport/pipeline setup
    # instead of serializing one more DB round-trip before the greeting;
    # run_bot awaits it right where the system messages are built.
    caller_phone = from_num if not is_outbound else to_num
    caller_history_task = (
        asyncio.create_task(get_caller_history(caller_phone, agent_id=agent.get("id") if agent else None))
        if caller_phone else None
    )

    if ccid and telnyx_api_key:
        asyncio.create_task(_start_recording(ccid, telnyx_api_key))

    _hung_up = False

    async def hangup_callback():
        nonlocal _hung_up
        if ccid and not _hung_up and telnyx_api_key:
            _hung_up = True
            logger.info(f"Sending Telnyx hangup for {ccid[:14]}…")
            await _telnyx_action(ccid, "hangup", telnyx_api_key)

    pipeline_mode, _ = await get_pipeline_config(user_id or "", agent=agent)
    _realtime_provider = REALTIME_PROVIDERS.get(pipeline_mode)
    is_realtime = _realtime_provider is not None and bool(os.getenv(_realtime_provider["api_key_env"]))

    runner_args = WebSocketRunnerArguments(websocket=websocket)
    runner_args.handle_sigint = False
    runner_args.pipeline_idle_timeout_secs = 60
    try:
        await bot(
            runner_args,
            hangup_callback=hangup_callback,
            is_outbound=is_outbound,
            agent=agent,
            db_call_id=db_call_id,
            call_control_id=ccid,
            caller_history_task=caller_history_task,
            caller_phone=caller_phone,
            user_id=user_id or "",
        )
    except Exception as exc:
        logger.exception(f"Call pipeline failed for ccid={ccid}: {exc}")
    finally:
        if caller_history_task is not None and not caller_history_task.done():
            caller_history_task.cancel()
        await _release_once(f"ws:active:{session_key}", _active_ws)

# ---------------------------------------------------------------------------
# Outbound dial
# ---------------------------------------------------------------------------

@router.post("/dial")
async def dial(
    request: Request,
    to: str = Form(...),
    from_: str | None = Form(default=None, alias="from"),
    user_id: str = Depends(get_current_user),
):
    user_row = await get_user_settings(user_id)
    if not user_row or not user_row.get("telnyx_api_key"):
        raise HTTPException(400, "Telnyx API key not configured — add it in Settings")

    telnyx_api_key = user_row["telnyx_api_key"]

    to_number = to.strip()
    if not _E164_RE.match(to_number):
        raise HTTPException(400, "Phone number must be E.164 format, e.g. +923001234567")

    caller = (from_ or "").strip()
    if not caller:
        raise HTTPException(400, "Caller number required — pass 'from' or configure agent number")
    if not _E164_RE.match(caller):
        raise HTTPException(400, "From number must be E.164 format, e.g. +923001234567")
    if to_number == caller:
        raise HTTPException(400, "Cannot dial the bot's own number")

    agent = await get_agent_by_number(caller, user_id)
    telnyx_app_id = (agent.get("telnyx_app_id") if agent else None)
    if not telnyx_app_id:
        raise HTTPException(400, "No Telnyx App ID configured for this agent")

    # Explicitly set the webhook URL on the dial request instead of relying on
    # the Connection's default — outbound calls have been observed to not fire
    # ANY webhook (not even call.initiated) when left to the connection default,
    # even though the same Connection reliably delivers webhooks for inbound.
    webhook_token = user_row.get("webhook_token")
    call_payload = {
        "connection_id": telnyx_app_id,
        "to": to_number,
        "from": caller,
        "answering_machine_detection": "detect",
    }
    if settings.public_host and webhook_token:
        call_payload["webhook_url"] = f"https://{settings.public_host}/webhook/{webhook_token}"
    else:
        logger.warning("Dialing without explicit webhook_url — PUBLIC_HOST or webhook_token missing")

    try:
        status, body = await _telnyx_post("/calls", call_payload, telnyx_api_key)
    except Exception as exc:
        logger.error(f"Telnyx dial request failed to reach Telnyx: {exc}")
        raise HTTPException(502, detail="Could not reach Telnyx — check your network and try again.")

    if status >= 300:
        msg = _telnyx_error_message(status, body)
        logger.warning(f"Dial rejected by Telnyx [{status}]: {msg}")
        # Normalize odd upstream codes to a valid client/server error status.
        http_status = status if 400 <= status < 600 else 502
        raise HTTPException(http_status, detail=msg)

    outbound_ccid = (body.get("data") or {}).get("call_control_id") if isinstance(body, dict) else None
    if outbound_ccid:
        agent_id = agent.get("id") if agent else None
        # Awaited for the same reason as the inbound path: the WS handler must
        # find this row, or the call loses transcripts/metrics/extraction.
        await create_call(
            call_control_id=outbound_ccid,
            direction="outbound",
            from_number=caller,
            to_number=to_number,
            agent_id=agent_id,
            user_id=user_id,
        )
        await _register_outbound(outbound_ccid, caller)
        logger.info(f"Outbound call registered: ccid={outbound_ccid[:14]}… to={to_number}")

    return JSONResponse({"ok": True, "telnyx": body})


# ---------------------------------------------------------------------------
# Public Live Call Widget endpoint (No auth required for demo)
# ---------------------------------------------------------------------------

class PublicCallPayload(BaseModel):
    phone_number: str
    agent_id: str | None = "sana_bank"
    language: str | None = "ur"

@router.post("/api/public-call")
async def public_call(payload: PublicCallPayload):
    raw_number = payload.phone_number.strip()
    to_number = normalize_phone(raw_number) or raw_number
    if not _E164_RE.match(to_number):
        raise HTTPException(400, detail="Phone number must be in E.164 format, e.g. +923001234567")

    caller = settings.telnyx_from_number or "+12029196011"
    if to_number == caller:
        raise HTTPException(400, detail="Cannot dial the bot's own number")

    agent = await get_agent_by_telnyx_number_any_user(caller)
    user_id = agent.get("user_id") if agent else ""
    user_row = await get_user_settings(user_id) if user_id else None

    telnyx_api_key = (user_row.get("telnyx_api_key") if user_row else None) or settings.telnyx_api_key
    if not telnyx_api_key:
        raise HTTPException(400, detail="Telnyx API key not configured on server")

    telnyx_app_id = (agent.get("telnyx_app_id") if agent else None) or (user_row.get("telnyx_app_id") if user_row else None) or settings.telnyx_app_id
    if not telnyx_app_id:
        raise HTTPException(400, detail="No Telnyx App ID configured for this agent")

    webhook_token = user_row.get("webhook_token") if user_row else None
    call_payload = {
        "connection_id": telnyx_app_id,
        "to": to_number,
        "from": caller,
        "answering_machine_detection": "detect",
    }
    if settings.public_host and webhook_token:
        call_payload["webhook_url"] = f"https://{settings.public_host}/webhook/{webhook_token}"
    elif settings.public_host:
        call_payload["webhook_url"] = f"https://{settings.public_host}/webhook"
    else:
        logger.warning("Public dial without explicit webhook_url — PUBLIC_HOST missing")

    try:
        status, body = await _telnyx_post("/calls", call_payload, telnyx_api_key)
    except Exception as exc:
        logger.error(f"Telnyx public dial request failed: {exc}")
        raise HTTPException(502, detail="Could not reach Telnyx — please check network.")

    if status >= 300:
        msg = _telnyx_error_message(status, body)
        logger.warning(f"Public dial rejected by Telnyx [{status}]: {msg}")
        http_status = status if 400 <= status < 600 else 502
        raise HTTPException(http_status, detail=msg)

    outbound_ccid = (body.get("data") or {}).get("call_control_id") if isinstance(body, dict) else None
    if outbound_ccid:
        agent_id = agent.get("id") if agent else None
        await create_call(
            call_control_id=outbound_ccid,
            direction="outbound",
            from_number=caller,
            to_number=to_number,
            agent_id=agent_id,
            user_id=user_id or "",
        )
        await _register_outbound(outbound_ccid, caller)
        logger.info(f"Public outbound call successfully placed: ccid={outbound_ccid[:14]}… to={to_number}")

    return JSONResponse({
        "ok": True,
        "status": "initiated",
        "message": f"Calling {to_number} from {caller} via Telnyx.",
        "caller_id": caller,
        "call_control_id": outbound_ccid,
        "telnyx": body,
    })


@router.get("/api/public-call/status")
async def public_call_status(ccid: str):
    """Check the real-time status of a public demo call."""
    if not ccid:
        raise HTTPException(400, detail="ccid parameter is required")
    data = await get_call_status_by_ccid(ccid)
    if not data:
        return {"status": "dialing"}
    return {
        "status": data.get("status") or "unknown",
        "duration_seconds": data.get("duration_seconds") or 0,
    }


class PublicFeedbackPayload(BaseModel):
    rating: int
    comment: Optional[str] = None
    call_control_id: Optional[str] = None
    phone_number: Optional[str] = None
    tags: Optional[list[str]] = None


@router.post("/api/public-call/feedback")
async def public_call_feedback(payload: PublicFeedbackPayload):
    """Save user rating and feedback for a public demo call into the DB."""
    if not (1 <= payload.rating <= 5):
        raise HTTPException(400, detail="Rating must be between 1 and 5 stars")
    ok = await save_call_feedback(
        rating=payload.rating,
        comment=payload.comment,
        call_control_id=payload.call_control_id,
        phone_number=payload.phone_number,
        tags=payload.tags,
    )
    return {"ok": ok, "message": "Feedback received. Thank you!"}


class SalesLeadPayload(BaseModel):
    name: str
    email: str
    phone_number: str
    company_name: Optional[str] = None
    use_case: str
    call_volume: str
    notes: Optional[str] = None


@router.post("/api/sales-lead")
async def create_sales_lead(payload: SalesLeadPayload, background_tasks: BackgroundTasks):
    """Receive and save high-priority Talk to Sales inquiry into DB and trigger automated emails."""
    if not payload.name.strip():
        raise HTTPException(400, detail="Name is required")
    if not payload.email.strip() or "@" not in payload.email:
        raise HTTPException(400, detail="Valid email is required")
    if not payload.phone_number.strip():
        raise HTTPException(400, detail="Phone number is required")
    if not payload.use_case.strip():
        raise HTTPException(400, detail="Primary use case is required")
    if not payload.call_volume.strip():
        raise HTTPException(400, detail="Expected monthly call volume is required")

    name = payload.name.strip()
    email = payload.email.strip()
    phone_number = payload.phone_number.strip()
    company_name = payload.company_name.strip()
    use_case = payload.use_case.strip()
    call_volume = payload.call_volume.strip()
    notes = payload.notes.strip()

    ok = await save_sales_lead(
        name=name,
        email=email,
        phone_number=phone_number,
        company_name=company_name,
        use_case=use_case,
        call_volume=call_volume,
        notes=notes,
    )
    if not ok:
        raise HTTPException(500, detail="Could not save sales lead. Please try again.")

    # Trigger automated client confirmation email & internal sales team alert
    background_tasks.add_task(
        trigger_sales_lead_emails,
        name=name,
        email=email,
        phone_number=phone_number,
        company_name=company_name,
        use_case=use_case,
        call_volume=call_volume,
        notes=notes,
    )

    return {"ok": True, "message": "Thank you! Our enterprise sales team will contact you shortly."}


