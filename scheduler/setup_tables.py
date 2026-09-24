"""
One-time (idempotent, safe to re-run) setup for the scheduler:
  - creates `scheduled_jobs` and `job_run_log` if they don't already exist
  - adds `fetched_at`/`fetch_status` to `financial_calendar` if missing
  - seeds/updates the five default job rows (upserted by job_key)

There's no migration tool in this repo (tables are created directly against
the shared Postgres DB), so this plays that role for the scheduler's own
tables.

Usage:
    python -m scheduler.setup_tables
"""
from __future__ import annotations

import datetime as _dt

from dotenv import load_dotenv
from sqlalchemy import text

from db.base import Base
from db.models.job_run_log import JobRunLog
from db.models.scheduled_job import ScheduledJob
from db.session import SessionLocal, engine

load_dotenv()

DEFAULT_JOBS = [
    dict(
        job_key="update_symbols",
        description="Upsert `symbols` from GDFL GetInstruments.",
        run_time=_dt.time(16, 15),
        depends_on_job_key=None,
    ),
    dict(
        job_key="update_financial_calendar",
        description="Upsert `financial_calendar` from GDFL GetResultsCalendar.",
        run_time=_dt.time(16, 20),
        depends_on_job_key="update_symbols",
    ),
    dict(
        job_key="update_ohlcv",
        description="Fetch daily EOD OHLCV into `price_snapshots`.",
        run_time=_dt.time(16, 35),
        depends_on_job_key="update_financial_calendar",
    ),
    dict(
        job_key="fetch_financials_same_day",
        description="Fetch financial statements due today per `financial_calendar`.",
        run_time=_dt.time(23, 30),
        depends_on_job_key=None,
    ),
    dict(
        job_key="fetch_financials_retry",
        description="Retry yesterday's financial_calendar combos not yet fetched.",
        run_time=_dt.time(8, 30),
        depends_on_job_key=None,
    ),
]


def create_tables() -> None:
    Base.metadata.create_all(bind=engine, tables=[ScheduledJob.__table__, JobRunLog.__table__])


def add_financial_calendar_columns() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE financial_calendar ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE financial_calendar ADD COLUMN IF NOT EXISTS fetch_status VARCHAR"))


def seed_jobs() -> None:
    db = SessionLocal()
    try:
        for defaults in DEFAULT_JOBS:
            row = db.query(ScheduledJob).filter_by(job_key=defaults["job_key"]).first()
            if row is None:
                db.add(ScheduledJob(**defaults))
                print(f"  inserted {defaults['job_key']}")
            else:
                row.run_time = defaults["run_time"]
                row.depends_on_job_key = defaults["depends_on_job_key"]
                row.description = defaults["description"]
                print(f"  updated {defaults['job_key']}")
        db.commit()
    finally:
        db.close()


def main():
    print("Creating scheduled_jobs / job_run_log tables (if not already present)...")
    create_tables()
    print("Adding fetched_at/fetch_status columns to financial_calendar (if not already present)...")
    add_financial_calendar_columns()
    print("Seeding/updating default job rows...")
    seed_jobs()
    print("Done.")


if __name__ == "__main__":
    main()
