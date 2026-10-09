"""
Rebuilds a standalone/consolidated-by-period completeness report for every
ACCEPTED symbol in `symbols`, using listing-date-aware expected period
ranges instead of a single fixed range for every symbol:

  PNL (quarterly):      from max(FY2022-Q1, FY-quarter of listed_date) to FY2025-Q4
  BalanceSheet/CashFlow (annual, M12): from max(2022, FY-year of listed_date) to 2024

("FY-year" = Indian fiscal year start year: Apr-Mar, so a date in Jan-Mar
belongs to the FY that started the previous calendar year.) A symbol listed
after a given floor is not penalized for periods before it existed; a
symbol listed on/before the floor just uses the floor, same as before.

For each symbol, a (statement, year[, quarter]) combo counts "available"
for a given nature (consolidated/standalone) as soon as >=1 row exists in
the matching table for that (symbol_id, year, nature) -- same existence
check used by backfill_truly_missing_bs_cf.py/backfill_truly_missing_pnl.py
(frequency == "annual" for BS/CF, frequency == "quarterly" + period in
Q1-Q4 for PNL).

classification per symbol:
  BOTH              consolidated_complete AND standalone_complete
  ONLY_CONSOLIDATED consolidated_complete AND NOT standalone_complete
  ONLY_STANDALONE   standalone_complete AND NOT consolidated_complete
  NEITHER           otherwise
no_rating_consolidated = NOT consolidated_complete
no_rating_standalone   = NOT standalone_complete

A symbol listed after the report's own end period (2025 Q4 / year 2024)
has an empty expected-period set and is vacuously complete on both sides
(classified BOTH, 0 missing).

Usage:
    python generate_financials_period_report.py
    python generate_financials_period_report.py --output report.xlsx
    python generate_financials_period_report.py --symbols RELIANCE,TCS
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

import pandas as pd
from dotenv import load_dotenv

from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.pnl_financials import PnlFinancial
from db.models.symbol import Symbol
from db.session import SessionLocal
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

OUTPUT_PATH = "standalone_consolidated_report_by_period.xlsx"
SHEET_NAME = "All Symbols"

PNL_FLOOR = (2022, 1)
PNL_CEIL = (2025, 3)
BS_CF_FLOOR_YEAR = 2021
BS_CF_CEIL_YEAR = 2025

QUARTER_LABEL = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}


def fy_quarter(d: dt.date) -> tuple[int, int]:
    """(FY-start year, quarter 1-4) for a calendar date, Apr-Mar fiscal year."""
    if d.month >= 4:
        return d.year, (d.month - 4) // 3 + 1
    return d.year - 1, 4


def expected_pnl_quarters(listed_date: dt.date | None) -> list[tuple[int, int]]:
    start = max(PNL_FLOOR, fy_quarter(listed_date)) if listed_date else PNL_FLOOR
    if start > PNL_CEIL:
        return []
    quarters = []
    year, q = start
    while (year, q) <= PNL_CEIL:
        quarters.append((year, q))
        q += 1
        if q > 4:
            q = 1
            year += 1
    return quarters


def expected_bs_cf_years(listed_date: dt.date | None) -> list[int]:
    start_year = max(BS_CF_FLOOR_YEAR, fy_quarter(listed_date)[0]) if listed_date else BS_CF_FLOOR_YEAR
    if start_year > BS_CF_CEIL_YEAR:
        return []
    return list(range(start_year, BS_CF_CEIL_YEAR + 1))


def available_annual_year_keys(db, model) -> dict[str, set[tuple[int, int]]]:
    """{nature: {(symbol_id, year)}} for every annual row in `model`."""
    rows = (
        db.query(model.symbol_id, model.year, model.statement_type)
        .filter(model.frequency == "annual")
        .distinct()
        .all()
    )
    out: dict[str, set[tuple[int, int]]] = {"consolidated": set(), "standalone": set()}
    for symbol_id, year, nature in rows:
        if nature in out:
            out[nature].add((symbol_id, year))
    return out


def available_pnl_quarter_keys(db) -> dict[str, set[tuple[int, int]]]:
    """{nature: {(symbol_id, year)}} keyed per quarter label for pnl_financials."""
    rows = (
        db.query(PnlFinancial.symbol_id, PnlFinancial.year, PnlFinancial.period, PnlFinancial.statement_type)
        .filter(PnlFinancial.frequency == "quarterly")
        .distinct()
        .all()
    )
    out: dict[str, dict[int, set[tuple[int, int]]]] = {
        "consolidated": {1: set(), 2: set(), 3: set(), 4: set()},
        "standalone": {1: set(), 2: set(), 3: set(), 4: set()},
    }
    label_to_q = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    for symbol_id, year, period, nature in rows:
        q = label_to_q.get(period)
        if q is None or nature not in out:
            continue
        out[nature][q].add((symbol_id, year))
    return out


def build_report(db, symbols: list[Symbol]) -> pd.DataFrame:
    bs_keys = available_annual_year_keys(db, BalanceSheetFinancial)
    cf_keys = available_annual_year_keys(db, CashflowFinancial)
    pnl_keys = available_pnl_quarter_keys(db)

    rows = []
    for symbol in symbols:
        listed_date = symbol.listed_date.date() if symbol.listed_date else None
        pnl_periods = expected_pnl_quarters(listed_date)
        bs_cf_years = expected_bs_cf_years(listed_date)

        missing = {"consolidated": [], "standalone": []}
        missing_by_stmt = {
            "consolidated": {"BS": [], "CF": [], "PNL": []},
            "standalone": {"BS": [], "CF": [], "PNL": []},
        }
        either_missing = []
        either_missing_by_stmt = {"BS": [], "CF": [], "PNL": []}
        for year in bs_cf_years:
            for label, keys in (("BS", bs_keys), ("CF", cf_keys)):
                for nature in ("consolidated", "standalone"):
                    if (symbol.id, year) not in keys[nature]:
                        missing[nature].append(f"{label}:{year}")
                        missing_by_stmt[nature][label].append(str(year))
                if (symbol.id, year) not in keys["consolidated"] and (symbol.id, year) not in keys["standalone"]:
                    either_missing.append(f"{label}:{year}")
                    either_missing_by_stmt[label].append(str(year))
        for year, q in pnl_periods:
            for nature in ("consolidated", "standalone"):
                if (symbol.id, year) not in pnl_keys[nature][q]:
                    missing[nature].append(f"PNL:{year}{QUARTER_LABEL[q]}")
                    missing_by_stmt[nature]["PNL"].append(f"{year}{QUARTER_LABEL[q]}")
            if (symbol.id, year) not in pnl_keys["consolidated"][q] and (symbol.id, year) not in pnl_keys["standalone"][q]:
                either_missing.append(f"PNL:{year}{QUARTER_LABEL[q]}")
                either_missing_by_stmt["PNL"].append(f"{year}{QUARTER_LABEL[q]}")

        cons_complete = not missing["consolidated"]
        stan_complete = not missing["standalone"]
        if cons_complete and stan_complete:
            classification = "BOTH"
        elif cons_complete:
            classification = "ONLY_CONSOLIDATED"
        elif stan_complete:
            classification = "ONLY_STANDALONE"
        else:
            classification = "NEITHER"

        rows.append({
            "symbol_id": symbol.id,
            "symbol": symbol.symbol,
            "name": symbol.name,
            "listed_date": listed_date,
            "consolidated_complete": cons_complete,
            "standalone_complete": stan_complete,
            "classification": classification,
            "no_rating_consolidated": not cons_complete,
            "no_rating_standalone": not stan_complete,
            "consolidated_missing_count": len(missing["consolidated"]),
            "standalone_missing_count": len(missing["standalone"]),
            "consolidated_missing_periods": ", ".join(missing["consolidated"]) or None,
            "standalone_missing_periods": ", ".join(missing["standalone"]) or None,
            "either_nature_complete": not either_missing,
            "either_nature_missing_count": len(either_missing),
            "either_nature_missing_periods": ", ".join(either_missing) or None,
            "pnl_missing_quarters": ", ".join(either_missing_by_stmt["PNL"]) or None,
            "balance_sheet_missing_years": ", ".join(either_missing_by_stmt["BS"]) or None,
            "cashflow_missing_years": ", ".join(either_missing_by_stmt["CF"]) or None,
            "pnl_missing_quarters_standalone": ", ".join(missing_by_stmt["standalone"]["PNL"]) or None,
            "balance_sheet_missing_years_standalone": ", ".join(missing_by_stmt["standalone"]["BS"]) or None,
            "cashflow_missing_years_standalone": ", ".join(missing_by_stmt["standalone"]["CF"]) or None,
            "pnl_missing_quarters_consolidated": ", ".join(missing_by_stmt["consolidated"]["PNL"]) or None,
            "balance_sheet_missing_years_consolidated": ", ".join(missing_by_stmt["consolidated"]["BS"]) or None,
            "cashflow_missing_years_consolidated": ", ".join(missing_by_stmt["consolidated"]["CF"]) or None,
        })
    return pd.DataFrame(rows)


def write_report(df: pd.DataFrame, output: str) -> None:
    total = len(df)
    counts = df["classification"].value_counts()
    both = int(counts.get("BOTH", 0))
    only_cons = int(counts.get("ONLY_CONSOLIDATED", 0))
    only_stan = int(counts.get("ONLY_STANDALONE", 0))
    neither = int(counts.get("NEITHER", 0))
    either_complete = int(df["either_nature_complete"].sum())

    summary_rows = [
        {"metric": "Total symbols", "count": total},
        {"metric": "BOTH", "count": both},
        {"metric": "ONLY_CONSOLIDATED", "count": only_cons},
        {"metric": "ONLY_STANDALONE", "count": only_stan},
        {"metric": "NEITHER", "count": neither},
        {"metric": "Only Consolidated + Both (consolidated_complete)", "count": only_cons + both},
        {"metric": "Only Standalone + Both (standalone_complete)", "count": only_stan + both},
        {"metric": "No Rating Consolidated (total - consolidated_complete)", "count": total - (only_cons + both)},
        {"metric": "No Rating Standalone (total - standalone_complete)", "count": total - (only_stan + both)},
        {"metric": "Consolidated-priority total (found in either nature)", "count": total - neither},
        {"metric": "Either-nature complete (no period missing in both)", "count": either_complete},
        {"metric": "Either-nature incomplete (>=1 period missing in both)", "count": total - either_complete},
    ]
    summary = pd.DataFrame(summary_rows)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
        df[df["classification"] == "BOTH"].to_excel(writer, sheet_name="Both", index=False)
        df[df["classification"] == "ONLY_STANDALONE"].to_excel(writer, sheet_name="Only Standalone", index=False)
        df[df["classification"] == "ONLY_CONSOLIDATED"].to_excel(writer, sheet_name="Only Consolidated", index=False)
        df[df["no_rating_consolidated"]].to_excel(writer, sheet_name="No Rating Consolidated", index=False)
        df[df["no_rating_standalone"]].to_excel(writer, sheet_name="No Rating Standalone", index=False)
        df[~df["either_nature_complete"]].to_excel(writer, sheet_name="Either Nature Incomplete", index=False)

        standalone_cols = [
            "symbol_id", "symbol", "name", "listed_date",
            "standalone_missing_count",
            "pnl_missing_quarters_standalone",
            "balance_sheet_missing_years_standalone",
            "cashflow_missing_years_standalone",
        ]
        df[df["standalone_missing_count"] > 0][standalone_cols].to_excel(
            writer, sheet_name="Standalone Missing", index=False
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=OUTPUT_PATH)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    args = parser.parse_args()

    configure_logging("generate_financials_period_report")

    db = SessionLocal()
    try:
        query = db.query(Symbol).filter(Symbol.final_status == "ACCEPTED").order_by(Symbol.symbol.asc())
        if args.symbols:
            requested = {s.strip() for s in args.symbols.split(",") if s.strip()}
            query = query.filter(Symbol.symbol.in_(requested))
        symbols = query.all()
        logger.info("ACCEPTED symbols loaded: %s", len(symbols))

        df = build_report(db, symbols)
    finally:
        db.close()

    write_report(df, args.output)
    logger.info("Wrote %s symbol row(s) to %s", len(df), args.output)
    print(df["classification"].value_counts())


if __name__ == "__main__":
    main()
