"""
Maps each `scheduled_jobs.job_key` to the function scheduler/runner.py should
call. Each function takes the job's own DB session and returns
(status, summary_dict), where status is "success" / "failed" / "no_data_due".
Exceptions are left to propagate -- the runner catches them per-job.
"""
from __future__ import annotations

import datetime as _dt
import os

from sqlalchemy.orm import Session

import fetch_bhavcopy_symbols
import fetch_price_snapshots
import fetch_results_calendar
import fetch_todays_financials_and_stock_details as ftfasd
from fetch_results_calendar import IST


def _today_ist() -> _dt.date:
    return _dt.datetime.now(IST).date()


def run_update_symbols(db: Session) -> tuple[str, dict]:
    stats = fetch_bhavcopy_symbols.run(db=db)
    return "success", stats


def run_update_financial_calendar(db: Session) -> tuple[str, dict]:
    stats = fetch_results_calendar.run(db=db)
    return "success", stats


def run_update_ohlcv(db: Session) -> tuple[str, dict]:
    api_key = os.environ["GDFL_ACCESS_KEY"]
    stats = fetch_price_snapshots.run_snapshot_scraper(
        status="ACCEPTED",
        id_start=None,
        id_end=None,
        limit=None,
        days=fetch_price_snapshots.DEFAULT_DAYS,
        exchange=fetch_price_snapshots.DEFAULT_EXCHANGE,
        api_key=api_key,
        db=db,
    )
    status = "failed" if stats.get("error") else "success"
    return status, stats


def run_fetch_financials_same_day(db: Session) -> tuple[str, dict]:
    summary = ftfasd.run(db, run_date=_today_ist())
    status = "success" if summary["symbols_processed"] else "no_data_due"
    return status, summary


def run_fetch_financials_retry(db: Session) -> tuple[str, dict]:
    yesterday = _today_ist() - _dt.timedelta(days=1)
    summary = ftfasd.run(db, run_date=yesterday, only_unfetched=True)
    status = "success" if summary["symbols_processed"] else "no_data_due"
    return status, summary


JOB_FUNCTIONS = {
    "update_symbols": run_update_symbols,
    "update_financial_calendar": run_update_financial_calendar,
    "update_ohlcv": run_update_ohlcv,
    "fetch_financials_same_day": run_fetch_financials_same_day,
    "fetch_financials_retry": run_fetch_financials_retry,
}
