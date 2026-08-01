#!/usr/bin/env python3
# scripts/run_scheduled_exports.py
# =============================================================================
# Batch 74 — Scheduled export worker (optional, run by cron)
# -----------------------------------------------------------------------------
# The app manages the schedule registry (report_schedules). This script is what
# actually DELIVERS: run it from cron (e.g. hourly). It finds active schedules
# whose frequency is due, generates the report file, emails it to the
# recipients, and stamps last_run_at.
#
# It is intentionally dependency-light and self-contained. Configure SMTP via
# environment variables; if SMTP is not configured it logs what it *would* send
# (dry-run) so the feature is safe to enable incrementally.
#
# Cron example (hourly):
#   0 * * * * cd /path/to/isfc && python scripts/run_scheduled_exports.py >> logs/exports.log 2>&1
#
# Env:
#   DATABASE_URL         SQLAlchemy URL (same as the app)
#   SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS SMTP_FROM
#   APP_BASE_URL         e.g. http://localhost:8000  (used to fetch the export)
# =============================================================================

import csv
import io
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage

try:
    from sqlalchemy import create_engine, text
except Exception:
    print("sqlalchemy required"); sys.exit(1)


def due(frequency: str, last_run_at) -> bool:
    if last_run_at is None:
        return True
    now = datetime.utcnow()
    delta = now - last_run_at
    return {
        "Daily": delta >= timedelta(days=1),
        "Weekly": delta >= timedelta(days=7),
        "Monthly": delta >= timedelta(days=30),
    }.get(frequency, delta >= timedelta(days=7))


def send_email(recipients, subject, body, attachment_name, attachment_bytes):
    host = os.getenv("SMTP_HOST")
    if not host:
        print(f"[dry-run] would email {recipients}: {subject} ({attachment_name})")
        return
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM", "noreply@isfc.local")
    msg["To"] = recipients
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(attachment_bytes, maintype="text", subtype="csv", filename=attachment_name)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
        s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
        s.send_message(msg)
    print(f"[sent] {recipients}: {subject}")


def main():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set"); sys.exit(1)
    engine = create_engine(url)
    with engine.connect() as db:
        schedules = db.execute(text(
            "SELECT * FROM report_schedules WHERE is_active = 1"
        )).mappings().all()
        for s in schedules:
            if not due(s["frequency"], s["last_run_at"]):
                continue
            # NOTE: reuse the app's export by hitting its endpoint, or run the SQL
            # directly here. Simplest: call the running app's export URL.
            base = os.getenv("APP_BASE_URL", "http://localhost:8000")
            try:
                import urllib.request
                u = f"{base}/reports/export/{s['report_key']}?format={s['fmt']}"
                data = urllib.request.urlopen(u, timeout=30).read()
            except Exception as e:
                print(f"[skip] {s['report_key']}: fetch failed ({e})")
                continue
            fname = f"{s['report_key']}_{datetime.utcnow():%Y%m%d}.{s['fmt']}"
            if s["recipients"]:
                send_email(s["recipients"],
                           f"ISFC scheduled report: {s['report_label']}",
                           "Attached is your scheduled export from ISFC PIMS.",
                           fname, data)
            db.execute(text("UPDATE report_schedules SET last_run_at=:now WHERE id=:i"),
                       {"now": datetime.utcnow(), "i": s["id"]})
            db.commit()
    print("done")


if __name__ == "__main__":
    main()
