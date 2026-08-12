"""Transactional email via Resend (https://resend.com), used only by the
custom signup/password-reset code flows in app/api/auth_flows.py.

Uses a plain aiohttp POST rather than the `resend` package — that package is
sync-only (blocks the event loop), and the codebase already talks to external
HTTP APIs this way (see app/api/webhooks.py's _telnyx_post).
"""

import aiohttp
from loguru import logger

from app.core.config import settings

_RESEND_URL = "https://api.resend.com/emails"
_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


async def send_email(to: str, subject: str, html: str, text: str) -> bool:
    """Send one email via Resend. Returns True on success, False on any failure
    (never raises — callers treat email delivery as best-effort)."""
    if not settings.resend_api_key:
        logger.error("RESEND_API_KEY not configured — cannot send email.")
        return False

    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_RESEND_URL, json=payload, headers=headers) as r:
                if r.status >= 300:
                    body = (await r.text())[:300]
                    logger.error(f"Resend send failed ({r.status}): {body}")
                    return False
                return True
    except Exception as exc:
        logger.error(f"Resend send raised: {exc}")
        return False


def _code_email_html(heading: str, intro: str, code: str) -> str:
    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;">
      <h2 style="color:#111;font-size:20px;margin:0 0 12px;">{heading}</h2>
      <p style="color:#444;font-size:14px;line-height:1.5;margin:0 0 24px;">{intro}</p>
      <div style="font-family:monospace;font-size:32px;font-weight:700;letter-spacing:8px;color:#111;background:#f4f4f5;border-radius:10px;padding:16px 0;text-align:center;margin:0 0 24px;">
        {code}
      </div>
      <p style="color:#888;font-size:13px;line-height:1.5;margin:0;">
        This code expires shortly. If you didn't request this, you can safely ignore this email.
      </p>
    </div>
    """


async def send_verification_code_email(to: str, code: str) -> bool:
    html = _code_email_html(
        "Verify your email",
        "Enter this code to finish creating your Invenco account.",
        code,
    )
    text = f"Verify your email — your Invenco code is {code}. It expires shortly."
    return await send_email(to, "Verify your email — Invenco", html, text)


async def send_reset_code_email(to: str, code: str) -> bool:
    html = _code_email_html(
        "Reset your password",
        "Enter this code to reset your Invenco password.",
        code,
    )
    text = f"Reset your password — your Invenco code is {code}. It expires shortly."
    return await send_email(to, "Reset your password — Invenco", html, text)
