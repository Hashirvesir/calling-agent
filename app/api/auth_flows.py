"""Custom signup-verification / forgot-password code flows.

Supabase Auth's own email sending is bypassed entirely here: users are
created/updated via the Admin API (service_role key), and the 6-digit codes
are emailed through Resend from the invenco.pk domain instead, matching the
OTP-style UI already built in the frontend (verify-email / reset-password
pages). These routes are all pre-login and unauthenticated by design.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, EmailStr

from app.core.database import (
    confirm_auth_user,
    consume_auth_code,
    create_auth_code,
    create_auth_user,
    ensure_user_settings,
    find_auth_user_by_email,
    get_latest_auth_code,
    increment_auth_code_attempts,
    set_auth_user_password,
)
from app.services.email import send_reset_code_email, send_verification_code_email

router = APIRouter(prefix="/api/auth", tags=["auth-flows"])

_SIGNUP_TTL = timedelta(minutes=15)
_RESET_TTL = timedelta(minutes=10)
_RESEND_COOLDOWN = timedelta(seconds=30)
_MAX_ATTEMPTS = 6


def _make_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{email.strip().lower()}:{code}".encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(row: dict) -> datetime:
    val = row["expires_at"]
    return datetime.fromisoformat(val.replace("Z", "+00:00")) if isinstance(val, str) else val


async def _issue_code(email: str, purpose: str) -> tuple[str, bool, bool]:
    """Return (code, sent, db_ok). db_ok is False only on a genuine DB write
    failure — callers that must not strand the user (signup) check it and
    raise; cooldown-skips still report db_ok=True since nothing needed writing."""
    existing = await get_latest_auth_code(email, purpose)
    if existing and not existing["used"] and _now() < _expires_at(existing):
        if _now() - datetime.fromisoformat(str(existing["created_at"]).replace("Z", "+00:00")) < _RESEND_COOLDOWN:
            return "", False, True

    code = _make_code()
    row = await create_auth_code(email, purpose, _hash_code(email, code), _now() + (_SIGNUP_TTL if purpose == "signup" else _RESET_TTL))
    if row is None:
        return "", False, False
    return code, True, True


async def _check_code(email: str, purpose: str, code: str) -> tuple[bool, str]:
    """Validate a submitted code against the latest row. Returns (ok, error)."""
    row = await get_latest_auth_code(email, purpose)
    if not row or row["used"]:
        return False, "Code expired or already used — request a new one."
    if _now() > _expires_at(row):
        return False, "Code expired — request a new one."
    if row["attempts"] >= _MAX_ATTEMPTS:
        return False, "Too many incorrect attempts — request a new code."
    if row["code_hash"] != _hash_code(email, code):
        await increment_auth_code_attempts(purpose, row["id"], row["attempts"])
        return False, "Invalid code."
    return True, ""


# ── Signup verification ─────────────────────────────────────────────────────

class SignupBody(BaseModel):
    email: EmailStr
    password: str


@router.post("/signup")
async def signup(body: SignupBody):
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    existing = await find_auth_user_by_email(body.email)
    if existing and existing.email_confirmed_at:
        raise HTTPException(409, "This email is already registered. Try signing in.")

    if existing and not existing.email_confirmed_at:
        # Pending from an earlier, never-verified signup — refresh the
        # password (in case they mistyped it before) and re-send a code.
        await set_auth_user_password(existing.id, body.password)
    else:
        user = await create_auth_user(body.email, body.password)
        if not user:
            raise HTTPException(503, "Could not create account — try again.")

    code, sent, db_ok = await _issue_code(body.email, "signup")
    if not db_ok:
        raise HTTPException(503, "Could not start verification — try again.")
    if sent and not await send_verification_code_email(body.email, code):
        logger.error(f"Verification email failed to send to {body.email}")
    return {"ok": True}


class VerifyEmailBody(BaseModel):
    email: EmailStr
    code: str


@router.post("/verify-email")
async def verify_email(body: VerifyEmailBody):
    ok, error = await _check_code(body.email, "signup", body.code)
    if not ok:
        raise HTTPException(400, error)

    user = await find_auth_user_by_email(body.email)
    if not user:
        raise HTTPException(404, "Account not found — please sign up again.")

    if not await confirm_auth_user(user.id):
        raise HTTPException(503, "Could not verify account — try again.")

    row = await get_latest_auth_code(body.email, "signup")
    if row:
        await consume_auth_code("signup", row["id"])
    await ensure_user_settings(user.id)
    return {"ok": True}


class ResendBody(BaseModel):
    email: EmailStr


@router.post("/resend-verification")
async def resend_verification(body: ResendBody):
    user = await find_auth_user_by_email(body.email)
    if user and not user.email_confirmed_at:
        code, sent, _db_ok = await _issue_code(body.email, "signup")
        if sent:
            await send_verification_code_email(body.email, code)
    # Generic response either way — avoids confirming which emails exist.
    return {"ok": True}


# ── Forgot password ──────────────────────────────────────────────────────────

class ForgotPasswordBody(BaseModel):
    email: EmailStr


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordBody):
    user = await find_auth_user_by_email(body.email)
    if user and user.email_confirmed_at:
        code, sent, _db_ok = await _issue_code(body.email, "reset")
        if sent:
            await send_reset_code_email(body.email, code)
    # Always generic — do not reveal whether the email has an account.
    return {"ok": True}


class VerifyResetCodeBody(BaseModel):
    email: EmailStr
    code: str


@router.post("/verify-reset-code")
async def verify_reset_code(body: VerifyResetCodeBody):
    ok, error = await _check_code(body.email, "reset", body.code)
    if not ok:
        raise HTTPException(400, error)
    return {"ok": True}


class ResetPasswordBody(BaseModel):
    email: EmailStr
    code: str
    new_password: str


@router.post("/reset-password")
async def reset_password(body: ResetPasswordBody):
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    ok, error = await _check_code(body.email, "reset", body.code)
    if not ok:
        raise HTTPException(400, error)

    user = await find_auth_user_by_email(body.email)
    if not user:
        raise HTTPException(404, "Account not found.")

    if not await set_auth_user_password(user.id, body.new_password):
        raise HTTPException(503, "Could not reset password — try again.")

    row = await get_latest_auth_code(body.email, "reset")
    if row:
        await consume_auth_code("reset", row["id"])
    return {"ok": True}
