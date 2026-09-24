"""
Entry point invoked by cron every few minutes (see README/setup instructions
for the crontab line). Reads `scheduled_jobs` fresh on every tick, so timings/
enabled/dependency edits made directly in the DB take effect immediately with
no restart.

Per enabled job, in run_time order:
  - skipped if it already completed today (`last_run_date == today`)
  - skipped if `now` hasn't reached its `run_time` yet
  - skipped if it depends on another job that hasn't succeeded (or come back
    no_data_due) today yet
  - otherwise run, record the outcome on both `scheduled_jobs` (current state)
    and `job_run_log` (history), then email per notify_on_success/failure

One job raising never stops the tick -- each job gets its own try/except and
its own DB session.

Usage:
    python -m scheduler.runner
    python -m scheduler.runner --now "2026-09-24 16:15"
"""
from __future__ import annotations

import argparse
import datetime as _dt
import traceback

from dotenv import load_dotenv

from db.models.job_run_log import JobRunLog
from db.models.scheduled_job import ScheduledJob
from db.session import SessionLocal
from fetch_results_calendar import IST
from scheduler.jobs import JOB_FUNCTIONS
from scheduler.notifier import send_job_notification

load_dotenv()

DONE_STATUSES = ("success", "no_data_due")


def resolve_now(now_override: str | None) -> _dt.datetime:
    if now_override:
        naive = _dt.datetime.strptime(now_override, "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=IST)
    return _dt.datetime.now(IST)


def _dependency_satisfied(config_db, job: ScheduledJob, today: _dt.date) -> bool:
    if not job.depends_on_job_key:
        return True
    dep = config_db.query(ScheduledJob).filter_by(job_key=job.depends_on_job_key).first()
    return bool(dep and dep.last_run_date == today and dep.last_status in DONE_STATUSES)


def _format_summary(summary: dict | None) -> str:
    if not summary:
        return ""
    parts = []
    for key, value in summary.items():
        if key == "rejected" and value:
            parts.append(f"rejected: {len(value)} symbol(s)")
        elif key == "unmatched" and value:
            parts.append(f"unmatched: {len(value)}")
        elif key in ("rejected", "unmatched"):
            continue
        else:
            parts.append(f"{key}: {value}")
    return "; ".join(parts)


def _execute_job(job: ScheduledJob) -> tuple[str, dict, str | None]:
    job_fn = JOB_FUNCTIONS[job.job_key]
    job_db = SessionLocal()
    try:
        status, summary = job_fn(job_db)
        return status, summary, None
    except Exception:
        job_db.rollback()
        return "failed", {}, traceback.format_exc()
    finally:
        job_db.close()


def run_due_jobs(now: _dt.datetime) -> None:
    today = now.date()
    config_db = SessionLocal()
    try:
        jobs = (
            config_db.query(ScheduledJob)
            .filter(ScheduledJob.enabled.is_(True))
            .order_by(ScheduledJob.run_time.asc())
            .all()
        )
        for job in jobs:
            if job.last_run_date == today:
                continue
            if now.time() < job.run_time:
                continue
            if not _dependency_satisfied(config_db, job, today):
                print(f"[scheduler] {job.job_key}: waiting on dependency {job.depends_on_job_key!r}")
                continue
            if job.job_key not in JOB_FUNCTIONS:
                print(f"[scheduler] {job.job_key}: no job function registered, skipping")
                continue

            print(f"[scheduler] running {job.job_key}...")
            started_at = _dt.datetime.now(IST)
            status, summary, error_detail = _execute_job(job)
            finished_at = _dt.datetime.now(IST)
            summary_text = _format_summary(summary)

            job.last_run_date = today
            job.last_status = status
            job.last_summary = summary_text
            job.last_run_started_at = started_at
            job.last_run_finished_at = finished_at

            log_row = JobRunLog(
                job_key=job.job_key,
                run_date=today,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                summary=summary_text,
                error_detail=error_detail,
                email_sent=False,
            )
            config_db.add(log_row)
            config_db.commit()

            print(f"[scheduler] {job.job_key} -> {status}: {summary_text}")

            should_notify = (status in DONE_STATUSES and job.notify_on_success) or (
                status == "failed" and job.notify_on_failure
            )
            if should_notify:
                sent = send_job_notification(
                    job_key=job.job_key,
                    description=job.description,
                    status=status,
                    summary=summary,
                    error_detail=error_detail,
                    run_date=today,
                )
                log_row.email_sent = sent
                config_db.commit()
    finally:
        config_db.close()


def main():
    parser = argparse.ArgumentParser(description="Run any scheduled_jobs rows that are due right now.")
    parser.add_argument("--now", help="Override the clock, IST, format 'YYYY-MM-DD HH:MM' (for manual testing).")
    args = parser.parse_args()

    now = resolve_now(args.now)
    print(f"[scheduler] tick at {now.isoformat()}")
    run_due_jobs(now)


if __name__ == "__main__":
    main()
