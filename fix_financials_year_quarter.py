"""
One-off data fix: recompute `year`/`period` in `financials`,
`pnl_financials`, `balance_sheet_financials`, and `cashflow_financials` from
each row's own `date` column, for rows written/touched by recent fetch runs.

Why: fetch_historical_financials.py / fetch_historical_financial_stock_details.py
used to save `year`/`period` straight from the GDFL request params (the
FY-start year and period code you asked for) instead of from the filing's
actual DateOfEndOfReportingPeriod. Those normally agree, but wherever they
didn't, the stored year/quarter is wrong even though `date` (the real
period-end date) is correct. Both store paths now derive `year`/`period`
from `date` going forward (see financial_metrics.fy_year_and_quarter); this
script re-derives `year`/`period` for rows already written before that
fix, purely from their existing `date` column -- no API calls needed.

Usage:
    python fix_financials_year_quarter.py
    python fix_financials_year_quarter.py --since 2026-09-14
    python fix_financials_year_quarter.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as _dt

from dotenv import load_dotenv

from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.financials import Financial
from db.models.pnl_financials import PnlFinancial
from db.session import SessionLocal
from financial_metrics import fy_year_and_quarter

load_dotenv()

MODELS = [Financial, PnlFinancial, BalanceSheetFinancial, CashflowFinancial]

DEFAULT_SINCE = "2026-09-14"


def fix_model(db, model, since: _dt.datetime, dry_run: bool) -> tuple[int, int]:
    rows = db.query(model).filter(model.updated_at >= since).all()
    checked = len(rows)
    fixed = 0
    for row in rows:
        if row.date is None:
            continue

        fy_year, quarter = fy_year_and_quarter(row.date)
        is_annual = row.frequency == "annual"
        new_period = "" if is_annual else quarter

        if row.year == fy_year and (row.period or "") == new_period:
            continue

        fixed += 1
        if not dry_run:
            row.year = fy_year
            row.period = new_period

    if not dry_run and fixed:
        db.commit()
    return checked, fixed


def main():
    parser = argparse.ArgumentParser(
        description="Recompute year/period from `date` for financials rows touched since a given date."
    )
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help=f"Only fix rows with updated_at >= this date (YYYY-MM-DD). Default: {DEFAULT_SINCE}.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    args = parser.parse_args()

    since = _dt.datetime.strptime(args.since, "%Y-%m-%d")

    db = SessionLocal()
    try:
        for model in MODELS:
            checked, fixed = fix_model(db, model, since, args.dry_run)
            verb = "would fix" if args.dry_run else "fixed"
            print(f"{model.__tablename__}: checked {checked} row(s) updated since {args.since}, {verb} {fixed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
