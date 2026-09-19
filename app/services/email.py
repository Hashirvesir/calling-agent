"""Transactional email via Resend (https://resend.com), used only by the
custom signup/password-reset code flows in app/api/auth_flows.py.

Uses a plain aiohttp POST rather than the `resend` package — that package is
sync-only (blocks the event loop), and the codebase already talks to external
HTTP APIs this way (see app/api/webhooks.py's _telnyx_post).
"""

from datetime import datetime, timezone
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


def _sales_lead_client_html(
    name: str,
    company_name: str,
    use_case: str,
    call_volume: str,
    calendar_url: str,
) -> str:
    company_phrase = f" for <strong>{company_name}</strong>" if company_name else ""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0;padding:0;background-color:#0A0A0B;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#FAFAFA;">
      <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:#0A0A0B;padding:40px 16px;">
        <tr>
          <td align="center">
            <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width:580px;background-color:#111114;border:1px solid rgba(255,255,255,0.08);border-radius:16px;overflow:hidden;box-shadow:0 20px 60px -20px rgba(0,0,0,0.7);">
              <tr>
                <td style="padding:32px 32px 24px;border-bottom:1px solid rgba(255,255,255,0.06);">
                  <div style="display:inline-flex;align-items:center;gap:8px;">
                    <span style="font-size:18px;font-weight:700;color:#FAFAFA;letter-spacing:-0.02em;">Invenco</span>
                    <span style="font-size:11px;font-family:monospace;color:#71717A;letter-spacing:0.06em;">AI</span>
                  </div>
                </td>
              </tr>
              <tr>
                <td style="padding:32px;font-size:15px;line-height:1.6;color:#E4E4E7;">
                  <p style="margin:0 0 16px;font-size:16px;font-weight:600;color:#FAFAFA;">Hi {name},</p>
                  
                  <p style="margin:0 0 16px;">
                    Thank you for reaching out to us at <a href="https://ai.invenco.pk" style="color:#00E5A0;text-decoration:none;">ai.invenco.pk</a>.
                  </p>

                  <p style="margin:0 0 16px;">
                    We received your request regarding automated calling workflows{company_phrase} (focusing on <strong style="color:#FAFAFA;">{use_case}</strong>).
                  </p>

                  <p style="margin:0 0 24px;color:#A1A1AA;">
                    Our team is reviewing your requirements to map out the best pipeline setup (cascade logic, function calling, or ultra-low latency S2S models) tailored to your scale of <strong style="color:#FAFAFA;">{call_volume}</strong> calls/month.
                  </p>

                  <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:12px;padding:20px;margin-bottom:28px;">
                    <p style="margin:0 0 12px;font-weight:600;color:#FAFAFA;font-size:15px;">
                      What happens next?
                    </p>
                    <p style="margin:0 0 16px;color:#A1A1AA;font-size:14px;line-height:1.5;">
                      We want to show you an exact walkthrough of how the voice agent integrates with your workflow. To skip the back-and-forth emails, feel free to pick a time slot that works best for you:
                    </p>
                    <div style="text-align:center;margin:16px 0 8px;">
                      <a href="{calendar_url}" target="_blank" style="display:inline-block;background-color:#FAFAFA;color:#0A0A0B;padding:12px 28px;font-size:14px;font-weight:600;border-radius:8px;text-decoration:none;letter-spacing:-0.01em;">
                        👉 Schedule Your 30-Minute Technical Demo Here
                      </a>
                    </div>
                  </div>

                  <p style="margin:0 0 24px;color:#A1A1AA;font-size:14px;">
                    If you&apos;d like to test our voice latency right now, you can also interact directly with our live browser widget on our homepage: 
                    <a href="https://ai.invenco.pk" style="color:#00E5A0;text-decoration:underline;">https://ai.invenco.pk</a>
                  </p>

                  <p style="margin:0 0 8px;color:#E4E4E7;">Looking forward to speaking with you!</p>
                  <p style="margin:0;font-weight:600;color:#FAFAFA;">Best regards,</p>
                  <p style="margin:2px 0 0;color:#71717A;font-size:13px;">The ai.invenco.pk Team</p>
                </td>
              </tr>
              <tr>
                <td style="padding:20px 32px;background:rgba(255,255,255,0.02);border-top:1px solid rgba(255,255,255,0.06);font-size:12px;color:#71717A;">
                  ai.invenco.pk | Voice AI Infrastructure &amp; Calling Agents
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """


def _sales_lead_client_text(
    name: str,
    company_name: str,
    use_case: str,
    call_volume: str,
    calendar_url: str,
) -> str:
    company_phrase = f" for {company_name}" if company_name else ""
    return f"""Hi {name},

Thank you for reaching out to us at ai.invenco.pk.

We received your request regarding automated calling workflows{company_phrase} (focusing on {use_case}).

Our team is reviewing your requirements to map out the best pipeline setup (cascade logic, function calling, or ultra-low latency S2S models) tailored to your scale of {call_volume} calls/month.

What happens next? We want to show you an exact walkthrough of how the voice agent integrates with your workflow. To skip the back-and-forth emails, feel free to pick a time slot that works best for you:

👉 Schedule Your 30-Minute Technical Demo Here: {calendar_url}

If you'd like to test our voice latency right now, you can also interact directly with our live browser widget on our homepage: https://ai.invenco.pk

Looking forward to speaking with you!

Best regards,
The ai.invenco.pk Team
ai.invenco.pk | Voice AI Infrastructure & Calling Agents
"""


async def send_sales_lead_client_email(
    to: str,
    name: str,
    company_name: str,
    use_case: str,
    call_volume: str,
) -> bool:
    calendar_url = settings.sales_calendar_url or "https://calendly.com/hashirvesir123/30min"
    subject = "Thanks for reaching out to ai.invenco.pk – Next steps for your demo"
    html = _sales_lead_client_html(name, company_name, use_case, call_volume, calendar_url)
    text = _sales_lead_client_text(name, company_name, use_case, call_volume, calendar_url)
    return await send_email(to, subject, html, text)


def _sales_lead_admin_html(
    name: str,
    email: str,
    phone_number: str,
    company_name: str,
    use_case: str,
    call_volume: str,
    notes: str,
    submitted_at: str,
) -> str:
    clean_phone = "".join(c for c in phone_number if c.isdigit() or c == "+")
    wa_phone = "".join(c for c in phone_number if c.isdigit())
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin:0;padding:24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0A0A0B;color:#FAFAFA;">
      <div style="max-width:600px;margin:0 auto;background:#111114;border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:28px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;border-bottom:1px solid rgba(255,255,255,0.08);padding-bottom:16px;">
          <h2 style="margin:0;color:#FAFAFA;font-size:18px;">🔥 New Enterprise Sales Inquiry</h2>
          <span style="font-size:12px;color:#00E5A0;background:rgba(0,229,160,0.1);padding:4px 8px;border-radius:999px;font-weight:600;">HIGH PRIORITY</span>
        </div>

        <table width="100%" cellpadding="8" cellspacing="0" style="font-size:14px;line-height:1.5;">
          <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
            <td width="35%" style="color:#A1A1AA;font-weight:500;">Prospect Name:</td>
            <td style="color:#FAFAFA;font-weight:600;">{name}</td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
            <td style="color:#A1A1AA;font-weight:500;">Work Email:</td>
            <td><a href="mailto:{email}" style="color:#00E5A0;text-decoration:none;">{email}</a></td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
            <td style="color:#A1A1AA;font-weight:500;">Phone / WhatsApp:</td>
            <td>
              <a href="tel:{clean_phone}" style="color:#FAFAFA;text-decoration:none;font-family:monospace;">{phone_number}</a>
              &nbsp;•&nbsp;
              <a href="https://wa.me/{wa_phone}" target="_blank" style="color:#25D366;text-decoration:none;font-size:12px;">Chat on WhatsApp ↗</a>
            </td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
            <td style="color:#A1A1AA;font-weight:500;">Company:</td>
            <td style="color:#FAFAFA;">{company_name or 'N/A'}</td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
            <td style="color:#A1A1AA;font-weight:500;">Primary Use-Case:</td>
            <td style="color:#FAFAFA;font-weight:600;">{use_case}</td>
          </tr>
          <tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
            <td style="color:#A1A1AA;font-weight:500;">Monthly Call Volume:</td>
            <td style="color:#00E5A0;font-weight:600;">{call_volume}</td>
          </tr>
          <tr>
            <td style="color:#A1A1AA;font-weight:500;vertical-align:top;">Notes / Requirements:</td>
            <td style="color:#E4E4E7;white-space:pre-wrap;">{notes or 'None'}</td>
          </tr>
        </table>

        <div style="margin-top:24px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.08);display:flex;gap:12px;">
          <a href="mailto:{email}?subject=Re:%20Voice%20AI%20Inquiry%20-%20ai.invenco.pk" style="background:#FAFAFA;color:#0A0A0B;padding:10px 18px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;">
            Reply to Prospect
          </a>
          <a href="https://wa.me/{wa_phone}" target="_blank" style="background:#25D366;color:#FFFFFF;padding:10px 18px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;">
            Open WhatsApp
          </a>
        </div>

        <p style="margin-top:20px;font-size:11px;color:#71717A;">
          Inquiry received at {submitted_at} • ai.invenco.pk Lead Dispatcher
        </p>
      </div>
    </body>
    </html>
    """


def _sales_lead_admin_text(
    name: str,
    email: str,
    phone_number: str,
    company_name: str,
    use_case: str,
    call_volume: str,
    notes: str,
    submitted_at: str,
) -> str:
    return f"""🔥 New Enterprise Sales Inquiry ({use_case})

Prospect: {name}
Email: {email}
Phone / WhatsApp: {phone_number}
Company: {company_name or 'N/A'}
Primary Use-Case: {use_case}
Monthly Call Volume: {call_volume}
Notes: {notes or 'None'}

Received at: {submitted_at}
"""


async def send_sales_lead_admin_email(
    to: str,
    name: str,
    email: str,
    phone_number: str,
    company_name: str,
    use_case: str,
    call_volume: str,
    notes: str,
    submitted_at: str,
) -> bool:
    subject = f"🔥 New Sales Lead: {company_name or name} ({use_case})"
    html = _sales_lead_admin_html(name, email, phone_number, company_name, use_case, call_volume, notes, submitted_at)
    text = _sales_lead_admin_text(name, email, phone_number, company_name, use_case, call_volume, notes, submitted_at)
    return await send_email(to, subject, html, text)


async def trigger_sales_lead_emails(
    name: str,
    email: str,
    phone_number: str,
    company_name: str,
    use_case: str,
    call_volume: str,
    notes: str,
) -> None:
    """Trigger automated confirmation to prospect and alert to internal sales/admin team."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info(f"Triggering sales lead automated emails for {email} ({name})...")

    # 1. Send confirmation to prospect
    try:
        sent_client = await send_sales_lead_client_email(
            to=email,
            name=name,
            company_name=company_name,
            use_case=use_case,
            call_volume=call_volume,
        )
        if sent_client:
            logger.info(f"Sales lead confirmation email successfully sent to prospect {email}")
        else:
            logger.warning(f"Sales lead confirmation email failed for prospect {email}")
    except Exception as exc:
        logger.error(f"Error sending sales lead confirmation to {email}: {exc}")

    # 2. Send alert to internal admin/sales team
    admin_to = settings.sales_notification_email
    if admin_to:
        try:
            sent_admin = await send_sales_lead_admin_email(
                to=admin_to,
                name=name,
                email=email,
                phone_number=phone_number,
                company_name=company_name,
                use_case=use_case,
                call_volume=call_volume,
                notes=notes,
                submitted_at=now_str,
            )
            if sent_admin:
                logger.info(f"Sales lead admin alert successfully sent to {admin_to}")
            else:
                logger.warning(f"Sales lead admin alert failed for {admin_to}")
        except Exception as exc:
            logger.error(f"Error sending sales lead admin alert to {admin_to}: {exc}")
