"""Email notifications for sync runs.

Primary: Mailgun API
Fallback: SMTP (Gmail)

Recipients are configured via EMAIL_RECIPIENTS in .env (comma-separated).
"""

import base64
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from pathlib import Path
from typing import List, Optional

import requests

from src.config import Config
from src.logger import setup_logger

logger = setup_logger("wholescripts_sync.email")

SHEET_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}"
LOGO_PATH = Path(__file__).resolve().parent.parent / "public" / "Wholescripts Nightly Sync.png"


def send_sync_email(
    summary: dict,
    dry_run: bool = False,
    sheet_id: Optional[str] = None,
) -> None:
    """Send a sync summary email to all configured recipients."""
    recipients = Config.EMAIL_RECIPIENTS
    if not recipients:
        logger.warning("No EMAIL_RECIPIENTS configured — skipping email")
        return

    subject, html_body, text_body = _build_email(summary, dry_run, sheet_id)

    # Try Mailgun first
    if Config.MAILGUN_API_KEY and Config.MAILGUN_DOMAIN:
        try:
            _send_mailgun(recipients, subject, html_body, text_body)
            logger.info("Sync email sent via Mailgun to %s", ", ".join(recipients))
            return
        except Exception as exc:
            logger.warning("Mailgun send failed: %s — falling back to SMTP", exc)

    # Fallback: SMTP
    if Config.SMTP_USER and Config.SMTP_PASSWORD:
        try:
            _send_smtp(recipients, subject, html_body, text_body)
            logger.info("Sync email sent via SMTP to %s", ", ".join(recipients))
            return
        except Exception as exc:
            logger.error("SMTP send also failed: %s", exc)
    else:
        logger.error("No email credentials configured (Mailgun or SMTP)")


# ── Mailgun ─────────────────────────────────────────────────────────

def _send_mailgun(
    recipients: List[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    url = f"{Config.MAILGUN_API_URL}/{Config.MAILGUN_DOMAIN}/messages"
    files = []
    if LOGO_PATH.exists():
        files.append(("inline", ("logo.png", LOGO_PATH.read_bytes(), "image/png")))
    resp = requests.post(
        url,
        auth=("api", Config.MAILGUN_API_KEY),
        data={
            "from": Config.MAILGUN_FROM,
            "to": recipients,
            "subject": subject,
            "text": text_body,
            "html": html_body,
        },
        files=files or None,
        timeout=30,
    )
    resp.raise_for_status()


# ── SMTP ────────────────────────────────────────────────────────────

def _send_smtp(
    recipients: List[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = Config.SMTP_FROM
    msg["To"] = ", ".join(recipients)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    if LOGO_PATH.exists():
        img = MIMEImage(LOGO_PATH.read_bytes(), _subtype="png")
        img.add_header("Content-ID", "<logo.png>")
        img.add_header("Content-Disposition", "inline", filename="logo.png")
        msg.attach(img)

    with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
        if Config.SMTP_USE_TLS:
            server.starttls()
        server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
        server.sendmail(Config.SMTP_USER, recipients, msg.as_string())


# ── Email content ───────────────────────────────────────────────────

def _build_email(
    summary: dict,
    dry_run: bool,
    sheet_id: Optional[str],
) -> tuple:
    """Return (subject, html_body, text_body)."""
    now_et = datetime.now(timezone(timedelta(hours=-5)))
    timestamp = now_et.strftime("%B %d, %Y at %I:%M %p ET")
    date_short = now_et.strftime("%Y-%m-%d %I:%M %p ET")

    mode = "DRY RUN" if dry_run else "LIVE SYNC"
    total_ws = summary.get("total_ws_products", 0)
    total_woo = summary.get("total_woo_products", 0)
    matched = summary.get("matched", 0)
    updated = summary.get("updated", 0)
    skipped = summary.get("skipped", 0)
    missing = summary.get("missing_in_woo", 0)
    failed = summary.get("failed", 0)

    # Status
    if failed > 0:
        status_label = "Completed with Failures"
        status_color = "#dc3545"
    elif updated > 0:
        status_label = "Completed Successfully"
        status_color = "#28a745"
    else:
        status_label = "No Changes Needed"
        status_color = "#28a745"

    sheet_link = ""
    sheet_link_text = ""
    if sheet_id:
        url = SHEET_URL.format(sheet_id=sheet_id)
        sheet_link = url
        sheet_link_text = f"\nFull report: {url}\n"

    subject = f"Wholescripts Nightly Sync — {status_label} — {date_short}"

    # Context paragraph
    if dry_run:
        action_word = "would update"
        context_detail = (
            f"This was a <strong>dry run</strong> — no changes were applied to WooCommerce. "
            f"The sync identified <strong>{updated}</strong> product(s) that would be updated "
            f"if run in live mode."
        )
    else:
        action_word = "updated"
        if updated > 0:
            context_detail = (
                f"The sync successfully updated <strong>{updated}</strong> product(s) "
                f"in WooCommerce to match the latest Wholescripts catalog data."
            )
        else:
            context_detail = (
                "All matched products are already in sync — no updates were necessary."
            )

    if failed > 0:
        context_detail += (
            f" However, <strong>{failed}</strong> product(s) failed during processing "
            f"and may require attention."
        )

    # ── Plain text ──
    text_body = f"""Doctors Studio — Wholescripts Nightly Sync
{'=' * 50}

{timestamp}
Mode: {mode}
Status: {status_label}

The nightly sync between Wholescripts and WooCommerce has completed.
{"This was a dry run — no changes were applied." if dry_run else ""}

Sync Summary
------------
Wholescripts Products:  {total_ws}
WooCommerce Products:   {total_woo}
Matched by SKU:         {matched}
{"Would Update:" if dry_run else "Updated:":24s}{updated}
Skipped (no change):    {skipped}
Missing in WooCommerce: {missing}
Failed:                 {failed}
{sheet_link_text}
—
This is an automated notification from the Wholescripts Nightly Sync system.
Doctors Studio | doctorsstudio.com
"""

    # ── HTML ──
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin: 0; padding: 0; background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">

  <!-- Wrapper -->
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #ffffff; padding: 0;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08);">

          <!-- Logo Header -->
          <tr>
            <td align="center" style="background-color: #ffffff; padding: 0; border-bottom: 1px solid #e9ecef;">
              <img src="cid:logo.png" alt="Doctors Studio — Wholescripts Nightly Sync" width="400" height="147" style="display: block; max-width: 100%; height: auto;" />
            </td>
          </tr>

          <!-- Status Bar -->
          <tr>
            <td style="background-color: {status_color}; padding: 12px 40px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="color: #ffffff; font-size: 14px; font-weight: 600;">{status_label}</td>
                  <td style="color: rgba(255,255,255,0.85); font-size: 13px; text-align: right;">{mode}</td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body Content -->
          <tr>
            <td style="padding: 32px 40px;">

              <!-- Greeting & Context -->
              <p style="margin: 0 0 6px; font-size: 13px; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600;">Sync Report</p>
              <p style="margin: 0 0 4px; font-size: 16px; color: #212529; font-weight: 600;">{timestamp}</p>
              <p style="margin: 16px 0 24px; font-size: 14px; color: #495057; line-height: 1.6;">
                {context_detail}
              </p>

              <!-- Summary Table -->
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border: 1px solid #e9ecef; border-radius: 6px; overflow: hidden;">
                <tr style="background-color: #f8f9fa;">
                  <td style="padding: 10px 16px; font-size: 12px; font-weight: 700; color: #495057; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #e9ecef;" colspan="2">Sync Summary</td>
                </tr>
                <tr>
                  <td style="padding: 10px 16px; font-size: 14px; color: #495057; border-bottom: 1px solid #f0f0f0;">Wholescripts Products</td>
                  <td style="padding: 10px 16px; font-size: 14px; color: #212529; text-align: right; font-weight: 600; border-bottom: 1px solid #f0f0f0;">{total_ws}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 16px; font-size: 14px; color: #495057; border-bottom: 1px solid #f0f0f0;">WooCommerce Products</td>
                  <td style="padding: 10px 16px; font-size: 14px; color: #212529; text-align: right; font-weight: 600; border-bottom: 1px solid #f0f0f0;">{total_woo}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 16px; font-size: 14px; color: #495057; border-bottom: 1px solid #f0f0f0;">Matched by SKU</td>
                  <td style="padding: 10px 16px; font-size: 14px; color: #212529; text-align: right; font-weight: 600; border-bottom: 1px solid #f0f0f0;">{matched}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 16px; font-size: 14px; color: #495057; border-bottom: 1px solid #f0f0f0;">{"Would Update" if dry_run else "Updated"}</td>
                  <td style="padding: 10px 16px; font-size: 14px; color: #0066cc; text-align: right; font-weight: 600; border-bottom: 1px solid #f0f0f0;">{updated}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 16px; font-size: 14px; color: #495057; border-bottom: 1px solid #f0f0f0;">Skipped (no change)</td>
                  <td style="padding: 10px 16px; font-size: 14px; color: #6c757d; text-align: right; font-weight: 600; border-bottom: 1px solid #f0f0f0;">{skipped}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 16px; font-size: 14px; color: #495057; border-bottom: 1px solid #f0f0f0;">Missing in WooCommerce</td>
                  <td style="padding: 10px 16px; font-size: 14px; color: #e67e22; text-align: right; font-weight: 600; border-bottom: 1px solid #f0f0f0;">{missing}</td>
                </tr>
                <tr>
                  <td style="padding: 10px 16px; font-size: 14px; color: #495057;">Failed</td>
                  <td style="padding: 10px 16px; font-size: 14px; color: {'#dc3545' if failed else '#28a745'}; text-align: right; font-weight: 600;">{failed}</td>
                </tr>
              </table>

              <!-- Sheet Link -->
              {"<table role='presentation' width='100%%' cellpadding='0' cellspacing='0' style='margin-top: 28px;'><tr><td align='center'><a href='" + sheet_link + "' style='display: inline-block; background-color: #1a73e8; color: #ffffff; padding: 12px 32px; border-radius: 6px; text-decoration: none; font-size: 14px; font-weight: 600;'>View Full Report in Google Sheets</a></td></tr><tr><td align='center' style='padding-top: 8px;'><a href='" + sheet_link + "' style='font-size: 12px; color: #6c757d; text-decoration: none; word-break: break-all;'>" + sheet_link + "</a></td></tr></table>" if sheet_link else ""}

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color: #f8f9fa; padding: 20px 40px; border-top: 1px solid #e9ecef;">
              <p style="margin: 0; font-size: 12px; color: #868e96; line-height: 1.5;">
                This is an automated notification from the Wholescripts Nightly Sync system.<br/>
                <a href="https://doctorsstudio.com" style="color: #868e96; text-decoration: none;">Doctors Studio</a> &nbsp;|&nbsp; <a href="mailto:Support@doctorsstudio.com" style="color: #868e96; text-decoration: none;">Support@doctorsstudio.com</a>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""

    return subject, html_body, text_body
