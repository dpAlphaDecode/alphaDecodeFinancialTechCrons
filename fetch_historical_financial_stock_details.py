from __future__ import annotations

import argparse
import datetime as _dt
import math
import os
import re
import time
from functools import lru_cache

import pandas as pd
from dotenv import load_dotenv

from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.pnl_financials import PnlFinancial
from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financials import (
    DEFAULT_EXCHANGE,
    FULL_YEAR_PERIOD,
    QUARTER_LABELS,
    fetch_all_symbols,
    fetch_historical_financials,
)
from financial_metrics import _xbrl_filename, resolve_period_date
from gdfl import GDFLClient

load_dotenv()

EXCEL_MAPPING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Detailed_page_keys.xlsx")

# Sheet layout in Detailed_page_keys.xlsx: ProfitLoss and Balance-sheet each
# hold one sheet with one column per sector; Cashflow is split into one sheet
# per sector (line items differ too much by sector to share a sheet).
PNL_SHEET_NAME = "ProfitLoss"
BALANCE_SHEET_SHEET_NAME = "Balance-sheet"
CASHFLOW_SHEET_MAP = {
    "Banking": "Cashflow-Banking",
    "Life insurance": "Cashflow-Life Insurance",
    "Gen Insurance": "Cashflow-Gen Insurance",
    "Non-Banking": "Cashflow-Non Banking",
    "Industries": "Cashflow-Indutry",  # sic -- matches the actual sheet name
    "Other": "Cashflow-Other",
}

STATEMENT_TABLE_CONFIG = {
    "ProfitLoss": PnlFinancial,
    "BalanceSheet": BalanceSheetFinancial,
    "CashFlow": CashflowFinancial,
}

# Symbol -> sector override, used when a filing has no usable XbrlUrl
# (missing/blank) and the sector can't be sniffed from it. Ported from
# stock_detailed_financials_file.py's special_company_types.
SPECIAL_COMPANY_SECTORS = {
    "IMPAL": "Industries",
    "SPIC": "Industries",
    "SUNDARMFIN": "Non-Banking",
    "WHEELS": "Industries",
    "BHARATRAS": "Industries",
    "KARURVYSYA": "Banking",
    "SUPRIYA": "Industries",
    "COHANCE": "Industries",
    "AZAD": "Industries",
}


def _normalize_sector_name(col_name) -> str | None:
    col_name = str(col_name).strip().lower()
    if col_name.startswith("banking"):
        return "Banking"
    if col_name.startswith("life insurance"):
        return "Life insurance"
    if col_name.startswith("gen insurance") or col_name.startswith("general insurance"):
        return "Gen Insurance"
    if col_name.startswith("non-banking") or col_name.startswith("non banking"):
        return "Non-Banking"
    if col_name.startswith("industries") or col_name.startswith("industry"):
        return "Industries"
    if col_name.startswith("other"):
        return "Other"
    return None


def _load_sheet_mapping(sheet_name: str) -> dict[str, dict[str, str]]:
    """One column per sector -> {sector: {parameter: expression}}."""
    df = pd.read_excel(EXCEL_MAPPING_FILE, sheet_name=sheet_name)
    df = df.where(pd.notna(df), None)

    mapping: dict[str, dict[str, str]] = {}
    for col in df.columns:
        if str(col).strip() == "Parameter":
            continue
        sector = _normalize_sector_name(col)
        if sector is None:
            continue
        mapping[sector] = {}
        for _, row in df.iterrows():
            parameter = row["Parameter"]
            value = row[col]
            if parameter and value and str(value).strip() != "-":
                mapping[sector][parameter] = str(value).strip()
    return mapping


def _load_cashflow_mapping() -> dict[str, dict[str, str]]:
    """One sheet per sector -> {sector: {parameter: expression}}."""
    mapping: dict[str, dict[str, str]] = {}
    for sector, sheet_name in CASHFLOW_SHEET_MAP.items():
        df = pd.read_excel(EXCEL_MAPPING_FILE, sheet_name=sheet_name)
        df = df.where(pd.notna(df), None)
        value_column = df.columns[1]
        mapping[sector] = {}
        for _, row in df.iterrows():
            parameter = row["Parameter"]
            value = row[value_column]
            if parameter and value and str(value).strip() != "-":
                mapping[sector][parameter] = str(value).strip()
    return mapping


@lru_cache(maxsize=1)
def load_metric_mappings() -> dict[str, dict[str, dict[str, str]]]:
    """{"ProfitLoss": {...}, "BalanceSheet": {...}, "CashFlow": {...}}, each keyed by sector."""
    return {
        "ProfitLoss": _load_sheet_mapping(PNL_SHEET_NAME),
        "BalanceSheet": _load_sheet_mapping(BALANCE_SHEET_SHEET_NAME),
        "CashFlow": _load_cashflow_mapping(),
    }


def resolve_sector(
    consolidated_items: list[dict] | None,
    standalone_items: list[dict] | None,
    symbol: str,
) -> str | None:
    """Sector is sniffed from Consolidated's XbrlUrl filename first, then Standalone's."""
    filename = None
    if consolidated_items:
        filename = _xbrl_filename(consolidated_items)
    if not filename and standalone_items:
        filename = _xbrl_filename(standalone_items)

    if not filename:
        return SPECIAL_COMPANY_SECTORS.get(symbol)
    if "IFGI" in filename:
        return "Gen Insurance"
    if "NonBanking" in filename or "NBFC" in filename:
        return "Non-Banking"
    if "IFLI" in filename:
        return "Life insurance"
    if "IFBanking" in filename or filename.startswith("Banking"):
        return "Banking"
    if "indas" in filename.lower() or "Main_Ind_As" in filename:
        return "Industries"
    return "Other"


def _values_by_label(items: list[dict]) -> dict[str, object]:
    """First occurrence of each Label wins, matching GDFL's own field semantics."""
    values: dict[str, object] = {}
    for item in items:
        label = item.get("Label")
        if label and label not in values:
            values[label] = item.get("Value")
    return values


def _get_label_value(values_by_label: dict, label: str) -> float | None:
    value = values_by_label.get(label)
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _get_metric_value(values_by_label: dict, expression: str | None) -> float | None:
    """
    Evaluates a Detailed_page_keys.xlsx formula ("A+B", "A÷B", ...) against
    this filing's Label->Value data. Ported from
    stock_detailed_financials_file.py's get_metric_value(), operating on a
    dict instead of a per-CSV DataFrame.

    Unlike the original (which always defaults an unresolved label to 0, even
    when *every* label the formula references is missing), this returns None
    when none of the referenced labels are present, so the metric counts as
    "not available" and the caller falls back to Standalone instead of
    silently recording a wrong 0. A partially-available sum expression (e.g.
    "A+B" where only A is filed) still defaults the missing term to 0,
    exactly like the original.
    """
    if expression is None:
        return None
    expression = str(expression).strip()
    if not expression or expression == "-":
        return None

    expression = expression.replace("\n", " ").replace("\r", " ")
    expression = (
        expression.replace("÷", "/")
        .replace("×", "*")
        .replace("−", "-")
        .replace("–", "-")
        .replace("—", "-")
    )
    expression = re.sub(r"\s+", " ", expression).strip()

    tokens = set(_TOKEN_RE.findall(expression))
    if not tokens:
        return None

    values: dict[str, float] = {}
    found_any = False
    for token in tokens:
        value = _get_label_value(values_by_label, token)
        if value is not None:
            found_any = True
        values[token] = value if value is not None else 0.0
    if not found_any:
        return None

    safe_globals = {"__builtins__": {}, "abs": abs, "round": round, "min": min, "max": max, "math": math}
    try:
        result = eval(expression, safe_globals, values)  # noqa: S307 -- closed vocabulary of numeric tokens only
    except ZeroDivisionError:
        return None
    except Exception:
        return None

    if result is None:
        return None
    if isinstance(result, (int, float)):
        if math.isinf(result) or math.isnan(result):
            return None
    return float(result)


def extract_detailed_metrics(
    consolidated_items: list[dict] | None,
    standalone_items: list[dict] | None,
    sector_mapping: dict[str, str],
) -> tuple[dict[str, float], dict[str, str]]:
    """Resolves each sector metric from Consolidated first, Standalone as a per-metric fallback."""
    values_cons = _values_by_label(consolidated_items) if consolidated_items else {}
    values_stan = _values_by_label(standalone_items) if standalone_items else {}

    metrics: dict[str, float] = {}
    sources: dict[str, str] = {}
    for metric_name, expr in sector_mapping.items():
        value = _get_metric_value(values_cons, expr) if values_cons else None
        source = "consolidated"
        if value is None and values_stan:
            value = _get_metric_value(values_stan, expr)
            source = "standalone"
        if value is not None:
            metrics[metric_name] = value
            sources[metric_name] = source
    return metrics, sources


def upsert_detailed_metric(
    db,
    model,
    *,
    symbol_id: int,
    statement_type: str,
    date: _dt.date,
    frequency: str,
    period: str,
    metric_name: str,
    metric_value: float,
    year: int,
) -> None:
    row = (
        db.query(model)
        .filter_by(
            symbol_id=symbol_id,
            statement_type=statement_type,
            frequency=frequency,
            date=date,
            metric_name=metric_name,
        )
        .first()
    )
    if row is None:
        row = model(
            symbol_id=symbol_id,
            statement_type=statement_type,
            date=date,
            frequency=frequency,
            metric_name=metric_name,
        )
        db.add(row)
    row.metric_value = metric_value
    row.period = period
    row.year = year
    # Flush so a later upsert in the same commit (e.g. another period landing
    # on the same date) sees this row as existing -- session has autoflush=False.
    db.flush()


def store_detailed_financials_for_symbol(
    db,
    symbol_id: int,
    symbol: str,
    data: dict,
    rejected_symbols: dict[str, list[str]],
) -> int:
    mappings = load_metric_mappings()
    written = 0

    for stmt_type, by_year in data.items():
        model = STATEMENT_TABLE_CONFIG[stmt_type]
        stmt_mappings = mappings[stmt_type]

        for year, by_period in by_year.items():
            for period, by_nature in by_period.items():
                consolidated = by_nature.get("Consolidated", {})
                standalone = by_nature.get("Standalone", {})
                consolidated_items = consolidated.get("Value") if "error" not in consolidated else None
                standalone_items = standalone.get("Value") if "error" not in standalone else None
                if not consolidated_items and not standalone_items:
                    continue

                sector = resolve_sector(consolidated_items, standalone_items, symbol)
                if sector is None:
                    rejected_symbols.setdefault(symbol, []).append(
                        f"can not figure out sector from XbrlUrl for {stmt_type} {year} {period}"
                    )
                    continue

                sector_mapping = stmt_mappings.get(sector)
                if not sector_mapping:
                    rejected_symbols.setdefault(symbol, []).append(
                        f"no {stmt_type} mapping for sector={sector}"
                    )
                    continue

                metrics, sources = extract_detailed_metrics(consolidated_items, standalone_items, sector_mapping)
                if not metrics:
                    continue

                date = resolve_period_date(consolidated_items, standalone_items)
                if date is None:
                    rejected_symbols.setdefault(symbol, []).append(
                        f"no DateOfEndOfReportingPeriod/DateOfEndOfFinancialYear for {stmt_type} {year} {period}"
                    )
                    continue

                is_annual = period == FULL_YEAR_PERIOD
                frequency = "annual" if is_annual else "quarterly"
                stored_period = "" if is_annual else QUARTER_LABELS.get(period, period)

                for metric_name, value in metrics.items():
                    upsert_detailed_metric(
                        db,
                        model,
                        symbol_id=symbol_id,
                        statement_type=sources[metric_name],
                        date=date,
                        frequency=frequency,
                        period=stored_period,
                        metric_name=metric_name,
                        metric_value=value,
                        year=year,
                    )
                    written += 1
    return written


def main():
    parser = argparse.ArgumentParser(
        description="Fetch trailing 1-year detailed financial statements (per Detailed_page_keys.xlsx) "
                    "for every symbol in the DB into pnl_financials/balance_sheet_financials/cashflow_financials."
    )
    parser.add_argument("--symbols", help="Comma-separated symbol list to use instead of the DB (for testing).")
    parser.add_argument("--limit", type=int, help="Only process the first N symbols (for testing).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols (rate limiting).")
    args = parser.parse_args()

    load_metric_mappings()  # fail fast if the xlsx is missing/malformed, before hitting the API

    db = SessionLocal()
    try:
        if args.symbols:
            requested = [s.strip() for s in args.symbols.split(",") if s.strip()]
            db_symbols = {
                s.symbol: s for s in db.query(Symbol).filter(Symbol.symbol.in_(requested)).all()
            }
            targets = [
                (sym, DEFAULT_EXCHANGE, db_symbols[sym].id if sym in db_symbols else None)
                for sym in requested
            ]
        else:
            symbols = fetch_all_symbols()
            targets = [(s.symbol, DEFAULT_EXCHANGE, s.id) for s in symbols]

        if args.limit:
            targets = targets[: args.limit]

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        total = len(targets)
        for idx, (symbol, exchange, symbol_id) in enumerate(targets, start=1):
            print(f"[{idx}/{total}] Fetching {symbol} ({exchange})...")

            if symbol_id is None:
                rejected_symbols.setdefault(symbol, []).append("symbol not found in `symbols` table, skipped")
                continue

            data = fetch_historical_financials(symbol, exchange=exchange, client=client)
            written = store_detailed_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            db.commit()
            print(f"  stored/updated {written} metric rows")

            if args.sleep and idx < total:
                time.sleep(args.sleep)

        if rejected_symbols:
            print("\n===== Symbols with unresolved data =====")
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    print(f"  {symbol}: {reason}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
