"""
Fetches GDFL Standalone-nature data for exactly the (symbol, year, period,
statement) combos the "Standalone Missing" bucket needs -- recomputed
directly against the DB the same way generate_financials_period_report.py
does (not by re-parsing the xlsx), so this always reflects current data:

  PNL (quarterly):      FY2022-Q1 .. FY2025-Q3 (floor raised to the
                         symbol's own listing quarter when listed later)
  BalanceSheet/CashFlow (annual, M12): 2021 .. 2025 (same listing-date floor)

One GetFinancialResults call per combo, nature_of_report="Standalone" only
(never Consolidated -- this bucket is specifically what's missing on the
Standalone side, so there's no reason to spend an API call re-fetching
Consolidated here). GDFLClient already self-throttles to the account's
hourly cap and waits out a hard rate-limit response, so this is safe to run
end-to-end unattended.

GetFinancialResults is called with format="Csv" -- GDFL's own CSV rendering
(Label,Value,Decimals,UnitId,Description,Key per line item), the same shape
already seen in upload_data_from_files/*/Standalone/<Type>/<year+period>/<symbol>.csv
from an earlier manual pull. Each combo is:
  1) written VERBATIM (the literal response text, not re-rendered) to
     data/standalone/<year>/<year><period_code>/<symbol>_<StatementType>.csv
     (period_code is GDFL's own code: QJ/QS/QD/QM for PNL quarters, M12 for
     BS/CF; the StatementType suffix is needed because BS and CF share the
     same year+M12 folder). A combo GDFL has no data for (just a header row,
     no XbrlUrl/line items) writes nothing and is logged, not an empty file.
  2) parsed (via csv.DictReader, so each row is a {"Label":..., "Value":...}
     dict -- a drop-in match for the item dicts extract_detailed_metrics()/
     resolve_sector()/resolve_period_date() already expect from the JSON
     path) and written to the DB through that same pipeline, so results land
     in pnl_financials/balance_sheet_financials/cashflow_financials exactly
     like any other fetch.

Usage:
    python fetch_standalone_missing_from_report.py --plan-only
    python fetch_standalone_missing_from_report.py --limit 5
    python fetch_standalone_missing_from_report.py --symbols RELIANCE,TCS
    python fetch_standalone_missing_from_report.py
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os

from dotenv import load_dotenv

from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.pnl_financials import PnlFinancial
from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financial_stock_details import (
    STATEMENT_TABLE_CONFIG,
    extract_detailed_metrics,
    load_metric_mappings,
    resolve_sector,
    upsert_detailed_metric,
)
from fetch_historical_financials import DEFAULT_EXCHANGE, FULL_YEAR_PERIOD
from financial_metrics import fy_year_and_quarter, resolve_period_date
from gdfl import GDFLAPIError, GDFLClient
from generate_financials_period_report import (
    QUARTER_LABEL,
    available_annual_year_keys,
    available_pnl_quarter_keys,
    expected_bs_cf_years,
    expected_pnl_quarters,
)
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "standalone")
QUARTER_CODE = {1: "QJ", 2: "QS", 3: "QD", 4: "QM"}
STMT_FOR = {"BS": "BalanceSheet", "CF": "CashFlow", "PNL": "ProfitLoss"}


def load_standalone_missing_combos(db, symbols_filter: set[str] | None = None) -> dict[int, dict]:
    """
    {symbol_id: {"symbol": str, "combos": [(stmt_type, year, period_code), ...]}}
    for every ACCEPTED symbol with >=1 period missing on the Standalone side,
    over the same expected ranges generate_financials_period_report.py uses.
    """
    bs_keys = available_annual_year_keys(db, BalanceSheetFinancial)
    cf_keys = available_annual_year_keys(db, CashflowFinancial)
    pnl_keys = available_pnl_quarter_keys(db)

    query = db.query(Symbol).filter(Symbol.final_status == "ACCEPTED").order_by(Symbol.symbol.asc())
    if symbols_filter:
        query = query.filter(Symbol.symbol.in_(symbols_filter))

    out: dict[int, dict] = {}
    for symbol in query.all():
        listed_date = symbol.listed_date.date() if symbol.listed_date else None
        pnl_periods = expected_pnl_quarters(listed_date)
        bs_cf_years = expected_bs_cf_years(listed_date)

        combos: list[tuple[str, int, str]] = []
        for year in bs_cf_years:
            if (symbol.id, year) not in bs_keys["standalone"]:
                combos.append(("BalanceSheet", year, FULL_YEAR_PERIOD))
            if (symbol.id, year) not in cf_keys["standalone"]:
                combos.append(("CashFlow", year, FULL_YEAR_PERIOD))
        for year, q in pnl_periods:
            if (symbol.id, year) not in pnl_keys["standalone"][q]:
                combos.append(("ProfitLoss", year, QUARTER_CODE[q]))

        if combos:
            out[symbol.id] = {"symbol": symbol.symbol, "combos": combos}
    return out


def _write_raw_csv(symbol: str, year: int, period_code: str, stmt_type: str, raw_text: str) -> None:
    folder = os.path.join(DATA_ROOT, str(year), f"{year}{period_code}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{symbol}_{stmt_type}.csv")
    with open(path, "w", newline="") as f:
        f.write(raw_text)


def process_combo(db, symbol_id: int, symbol: str, stmt_type: str, year: int, period_code: str, client: GDFLClient) -> tuple[int, str | None]:
    """Fetches one (symbol, year, period_code) Standalone combo via format=Csv, writes the raw response + DB. Returns (metrics_written, skip_reason)."""
    try:
        raw_text = client.get_financial_results(
            exchange=DEFAULT_EXCHANGE,
            instrument_identifier=symbol,
            year=year,
            nature_of_report="Standalone",
            type=stmt_type,
            period=period_code,
            format="Csv",
        )
    except GDFLAPIError as exc:
        return 0, f"GDFLAPIError: {exc}"

    if not isinstance(raw_text, str) or not raw_text.strip():
        return 0, "empty response"

    items = list(csv.DictReader(io.StringIO(raw_text)))
    if not items:
        return 0, "empty Value"

    sector = resolve_sector(None, items, symbol)
    if sector is None:
        return 0, "sector unresolved"

    mappings = load_metric_mappings()
    sector_mapping = mappings[stmt_type].get(sector)
    if not sector_mapping:
        return 0, f"no {stmt_type} mapping for sector={sector}"

    metrics, _sources = extract_detailed_metrics(None, items, sector_mapping)
    if not metrics:
        return 0, "no mappable metrics in filing"

    date = resolve_period_date(None, items)
    if date is None:
        return 0, "no period-end date in filing"

    # Raw file is written only once we know the filing actually has usable
    # data -- a GDFL "nothing on file" response is just a bare header row,
    # which is as uninformative saved to disk as it is for extraction.
    _write_raw_csv(symbol, year, period_code, stmt_type, raw_text)

    model = STATEMENT_TABLE_CONFIG[stmt_type]
    is_annual = period_code == FULL_YEAR_PERIOD
    frequency = "annual" if is_annual else "quarterly"
    fy_year, quarter = fy_year_and_quarter(date)
    stored_period = "" if is_annual else quarter

    for metric_name, value in metrics.items():
        upsert_detailed_metric(
            db, model,
            symbol_id=symbol_id, statement_type="standalone", date=date,
            frequency=frequency, period=stored_period, metric_name=metric_name,
            metric_value=value, year=fy_year,
        )

    return len(metrics), None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--limit", type=int, help="Only process the first N symbols (for testing).")
    parser.add_argument("--plan-only", action="store_true", help="Print the call plan and exit, no network calls.")
    args = parser.parse_args()

    configure_logging("fetch_standalone_missing_from_report")
    load_metric_mappings()

    symbols_filter = {s.strip() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    db = SessionLocal()
    try:
        targets = load_standalone_missing_combos(db, symbols_filter)
        total_combos = sum(len(e["combos"]) for e in targets.values())
        print(f"Symbols with a Standalone gap: {len(targets)}")
        print(f"Total (symbol, year, period, statement) combos to fetch: {total_combos}")

        items = list(targets.items())
        if args.limit:
            items = items[: args.limit]
        targets = dict(items)
        if args.limit:
            total_combos = sum(len(e["combos"]) for e in targets.values())
            print(f"Limited to first {args.limit} symbol(s): {total_combos} combos")

        if args.plan_only:
            return

        client = GDFLClient()
        summary = {"combos": 0, "metrics_written": 0, "skipped": 0}
        skip_reasons: dict[str, int] = {}

        total = len(targets)
        for idx, (symbol_id, entry) in enumerate(targets.items(), start=1):
            symbol = entry["symbol"]
            logger.info("[%s/%s] %s: %s combo(s)", idx, total, symbol, len(entry["combos"]))
            for stmt_type, year, period_code in entry["combos"]:
                written, skip_reason = process_combo(db, symbol_id, symbol, stmt_type, year, period_code, client)
                summary["combos"] += 1
                summary["metrics_written"] += written
                if skip_reason:
                    summary["skipped"] += 1
                    skip_reasons[skip_reason] = skip_reasons.get(skip_reason, 0) + 1
            db.commit()

        print(f"\nDone. Combos processed: {summary['combos']}, metric rows written: {summary['metrics_written']}, skipped: {summary['skipped']}")
        if skip_reasons:
            print("Skip reasons:")
            for reason, count in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
                print(f"  {count:6d}  {reason}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
