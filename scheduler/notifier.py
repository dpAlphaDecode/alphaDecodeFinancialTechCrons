"""
Sends a per-job-run status email via Resend's HTTP API
(https://resend.com/docs/api-reference/emails/send-email) -- no SMTP server
or mailbox needed, just RESEND_API_KEY.

Reads RESEND_API_KEY / NOTIFY_FROM_EMAIL / NOTIFY_RECIPIENTS from the
environment (.env) on every call rather than at import time, so filling these
in later doesn't require restarting anything. Missing config, or any request
failure, is logged and swallowed -- a notification problem must never fail
the underlying job or crash the runner.
"""
from __future__ import annotations

import datetime as _dt
import html
import os

import requests

RESEND_API_URL = "https://api.resend.com/emails"


def _config() -> tuple[str | None, str | None, list[str]]:
    api_key = os.environ.get("RESEND_API_KEY") or None
    from_email = os.environ.get("NOTIFY_FROM_EMAIL") or None
    recipients = [r.strip() for r in os.environ.get("NOTIFY_RECIPIENTS", "").split(",") if r.strip()]
    return api_key, from_email, recipients


def _render_html(
    job_key: str,
    description: str | None,
    status: str,
    summary: dict | None,
    error_detail: str | None,
    run_date: _dt.date,
) -> str:
    summary = summary or {}
    rows = []
    for key, value in summary.items():
        if key == "rejected" and value:
            rows.append(f"<li><b>rejected</b>: {len(value)} symbol(s) with unresolved data</li>")
        elif key == "unmatched" and value:
            rows.append(f"<li><b>unmatched</b>: {len(value)} entr{'y' if len(value) == 1 else 'ies'}</li>")
        elif key in ("rejected", "unmatched"):
            continue
        else:
            rows.append(f"<li><b>{html.escape(str(key))}</b>: {html.escape(str(value))}</li>")

    error_html = ""
    if error_detail:
        error_html = f"<p style='color:#b00020'><b>Error:</b></p><pre>{html.escape(error_detail)}</pre>"

    return (
        "<div>"
        f"<p><b>Job:</b> {html.escape(job_key)}{' — ' + html.escape(description) if description else ''}</p>"
        f"<p><b>Run date (IST):</b> {run_date}</p>"
        f"<p><b>Status:</b> {status.upper()}</p>"
        f"<ul>{''.join(rows)}</ul>"
        f"{error_html}"
        "</div>"
    )


def send_job_notification(
    *,
    job_key: str,
    description: str | None,
    status: str,
    summary: dict | None,
    error_detail: str | None,
    run_date: _dt.date,
) -> bool:
    """Returns True if the email was accepted by Resend, False otherwise (never raises)."""
    api_key, from_email, recipients = _config()
    if not api_key or not from_email or not recipients:
        print(
            f"[notifier] email skipped for {job_key}: set RESEND_API_KEY, NOTIFY_FROM_EMAIL "
            "and NOTIFY_RECIPIENTS in .env to enable notifications"
        )
        return False

    subject = f"[Cron] {job_key} {status} — {run_date}"
    body_html = _render_html(job_key, description, status, summary, error_detail, run_date)

    try:
        response = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_email, "to": recipients, "subject": subject, "html": body_html},
            timeout=15,
        )
        if response.status_code >= 300:
            print(f"[notifier] Resend API error {response.status_code} for {job_key}: {response.text}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"[notifier] failed to send email for {job_key}: {exc}")
        return False
