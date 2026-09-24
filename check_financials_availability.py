"""
For every symbol in `symbols`, checks whether pnl_financials/
balance_sheet_financials/cashflow_financials already have data for a given
set of (GDFL year, GDFL quarter code) checks -- e.g. (2025, "QD") and
(2026, "QM") -- for Consolidated and Standalone separately. A check counts as
"Yes" as soon as a single metric_name row exists for that (symbol, year,
period, statement_type) combo in the table; it does not verify the full
metric set is present.

GDFL quarter code -> the `period` label these tables actually store
(same QUARTER_LABELS mapping fetch_historical_financial_stock_details.py
uses when writing rows): QJ->Q1, QS->Q2, QD->Q3, QM->Q4.

Note: some companies report semi-annually and are stored with period="M6"
instead of a quarterly label -- those will show "No" for a QD/QM check even
though they simply don't file on a quarterly cadence.

Usage:
    python check_financials_availability.py
    python check_financials_availability.py --checks 2025:QD,2026:QM
    python check_financials_availability.py --checks 2025:QD,2026:QM --output report.xlsx
    python check_financials_availability.py --symbols RELIANCE,TCS
"""
from __future__ import annotations

import argparse

import pandas as pd
from dotenv import load_dotenv

from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.pnl_financials import PnlFinancial
from db.models.symbol import Symbol
from db.session import SessionLocal

load_dotenv()

QUARTER_LABELS = {"QJ": "Q1", "QS": "Q2", "QD": "Q3", "QM": "Q4"}
NATURES = ("consolidated", "standalone")

TABLES = {
    "pnl": PnlFinancial,
    "balance_sheet": BalanceSheetFinancial,
    "cashflow": CashflowFinancial,
}

DEFAULT_CHECKS = "2025:QD,2026:QM"


def parse_checks(raw: str) -> list[tuple[int, str]]:
    checks = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        year_str, period_code = token.split(":")
        checks.append((int(year_str), period_code.strip().upper()))
    return checks


def available_keys(db, model, years: set[int]) -> set[tuple[int, int, str, str]]:
    """{(symbol_id, year, period, statement_type)} for every row whose year is one of `years`."""
    rows = (
        db.query(model.symbol_id, model.year, model.period, model.statement_type)
        .filter(model.year.in_(years))
        .distinct()
        .all()
    )
    return set(rows)


def build_report(db, symbols: list[Symbol], checks: list[tuple[int, str]]) -> pd.DataFrame:
    years = {year for year, _ in checks}
    available = {table_key: available_keys(db, model, years) for table_key, model in TABLES.items()}

    rows = []
    for symbol in symbols:
        row = {"symbol": symbol.symbol, "symbol_id": symbol.id}
        for year, period_code in checks:
            period_label = QUARTER_LABELS.get(period_code, period_code)
            tag = f"{year}{period_code}"
            for table_key in TABLES:
                for nature in NATURES:
                    key = (symbol.id, year, period_label, nature)
                    row[f"{table_key}_{tag}_{nature}"] = "Yes" if key in available[table_key] else "No"
        rows.append(row)
    return pd.DataFrame(rows)


def run(checks: list[tuple[int, str]], symbols_filter: set[str] | None = None) -> pd.DataFrame:
    db = SessionLocal()
    try:
        query = db.query(Symbol).order_by(Symbol.symbol.asc())
        if symbols_filter:
            query = query.filter(Symbol.symbol.in_(symbols_filter))
        symbols = query.all()
        return build_report(db, symbols, checks)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description="For every symbol, check whether pnl_financials/balance_sheet_financials/"
        "cashflow_financials have Consolidated and Standalone data for the given "
        "(GDFL year, GDFL quarter code) checks."
    )
    parser.add_argument(
        "--checks",
        default=DEFAULT_CHECKS,
        help="Comma-separated year:period_code pairs, e.g. '2025:QD,2026:QM' (QJ=Q1, QS=Q2, QD=Q3, QM=Q4).",
    )
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--output", default="financials_availability.xlsx", help="Output .xlsx path.")
    args = parser.parse_args()

    checks = parse_checks(args.checks)
    symbols_filter = {s.strip() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    df = run(checks, symbols_filter)
    df.to_excel(args.output, index=False)
    print(f"Wrote {len(df)} row(s) to {args.output}")

    for year, period_code in checks:
        period_label = QUARTER_LABELS.get(period_code, period_code)
        tag = f"{year}{period_code}"
        print(f"\n{tag} (FY{year} {period_label}):")
        for table_key in TABLES:
            for nature in NATURES:
                col = f"{table_key}_{tag}_{nature}"
                yes = int((df[col] == "Yes").sum())
                print(f"  {col}: {yes}/{len(df)} symbols have data")


if __name__ == "__main__":
    main()
