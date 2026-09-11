"""User settings API — Telnyx credentials per user."""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from typing import Optional

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import ensure_user_settings, get_user_settings, save_user_settings

router = APIRouter(tags=["settings"])


def _mask(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


@router.get("/api/settings")
async def get_settings(user_id: str = Depends(get_current_user)):
    try:
        row = await ensure_user_settings(user_id)
    except Exception as exc:
        logger.error(f"get_settings failed for user {user_id[:8]}…: {exc}")
        raise HTTPException(503, "Could not load settings — the database is unavailable. Try again.")
    host = settings.public_host or "localhost:7860"
    webhook_token = row.get("webhook_token", "") if row else ""
    telnyx_api_key = row.get("telnyx_api_key") or "" if row else ""
    telnyx_webhook_public_key = row.get("telnyx_webhook_public_key") or "" if row else ""
    return {
        "telnyx_api_key": _mask(telnyx_api_key),
        "telnyx_webhook_public_key": _mask(telnyx_webhook_public_key),
        "webhook_url": f"https://{host}/webhook/{webhook_token}" if webhook_token else "",
        "has_telnyx_api_key": bool(telnyx_api_key),
        "has_webhook_key": bool(telnyx_webhook_public_key),
    }


class SettingsUpdate(BaseModel):
    telnyx_api_key: Optional[str] = None
    telnyx_webhook_public_key: Optional[str] = None


@router.put("/api/settings")
async def update_settings(body: SettingsUpdate, user_id: str = Depends(get_current_user)):
    # Treat a submitted-but-blank field as "leave unchanged" so the user can't
    # accidentally wipe a saved key by submitting the masked form.
    new_api_key = (body.telnyx_api_key or "").strip() or None
    new_webhook_key = (body.telnyx_webhook_public_key or "").strip() or None

    try:
        current = await get_user_settings(user_id)
    except Exception as exc:
        logger.error(f"update_settings load failed for user {user_id[:8]}…: {exc}")
        raise HTTPException(503, "Could not reach the database. Try again.")

    api_key = new_api_key if new_api_key is not None else (current.get("telnyx_api_key") or "" if current else "")
    webhook_key = new_webhook_key if new_webhook_key is not None else (current.get("telnyx_webhook_public_key") or "" if current else "")

    try:
        row = await save_user_settings(user_id, api_key, webhook_key)
    except Exception as exc:
        logger.error(f"save_user_settings failed for user {user_id[:8]}…: {exc}")
        raise HTTPException(503, "Could not save settings — the database is unavailable. Try again.")

    if row is None:
        raise HTTPException(500, "Failed to save settings — please try again.")
    return {"ok": True}
