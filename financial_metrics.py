"""
Maps GDFL GetFinancialResults line items (the `Value` list of
{"Label": ..., "Value": ...} dicts) onto the fixed set of headline metrics
tracked in the `financials` table, and upserts them there.

The label a given headline metric lives under depends on which XBRL taxonomy
the company filed under (non-banking Ind-AS, banking, life insurance, general
insurance, "industry" Ind-AS, or the old/other AS format) -- that taxonomy is
identified from the filename of the `XbrlUrl` line item. Each metric mapping
value is a small expression over labels:
  - "A+B"   -> sum of the values found under label A and label B
  - "A/B" or "A#B" -> alternate spellings of the same concept across taxonomy
                       versions; use whichever label is present (first match)
A mapping expression is first split on "+" into additive terms, then each
term is split on "/" or "#" into alternate-label candidates.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
from decimal import Decimal, InvalidOperation

from db.models.financials import Financial

matric_mapping_non_banking = {
    "Equity Share Capital": "PaidUpValueOfEquityShareCapital",
    "Net Profit": "ProfitLossForPeriod/ProfitLossForThePeriod",
    "Face Value Of Equity Share Capital": "FaceValueOfEquityShareCapital",
    "sales": "Income/Revenue",
    "Share Holder’s Equity": "EquityAttributableToOwnersOfParent",
    "Reserve & Surplus": "OtherEquity",
    "Total Debt": "Borrowings",
    "Dividend Paid": "DividendsPaidClassifiedAsFinancingActivities",
}

matric_mapping_banking = {
    "Equity Share Capital": "PaidUpValueOfEquityShareCapital",
    "Net Profit": "ProfitLossForPeriod/ProfitLossForThePeriod",
    "Face Value Of Equity Share Capital": "FaceValueOfEquityShareCapital",
    "sales": "Income",
    "Share Holder’s Equity": "Capital+OtherEquity#ReservesAndSurplus",
    "Reserve & Surplus": "OtherEquity#ReservesAndSurplus",
    "Total Debt": "Borrowings",
    "Dividend Paid": "DividendsPaidClassifiedAsFinancingActivities",
}

matric_mapping_life_insurance = {
    "Equity Share Capital": "ShareCapital",
    "Net Profit": "ProfitLossAfterTaxAndExtraordinaryItems",
    "Face Value Of Equity Share Capital": "",
    "sales": "Income",
    "Share Holder’s Equity": "ShareholdersFunds",
    "Reserve & Surplus": "ReservesAndSurplus",
    "Total Debt": "Borrowings",
    "Dividend Paid": "DividendsPaidClassifiedAsFinancingActivities",
}

matric_mapping_gen_insurance = {
    "Equity Share Capital": "ShareCapital",
    "Net Profit": "ProfitLossAfterTax",
    "Face Value Of Equity Share Capital": "",
    "sales": "OperatingIncome",
    "Share Holder’s Equity": "ShareCapital+ReservesAndSurplus",
    "Reserve & Surplus": "ReservesAndSurplus",
    "Total Debt": "Borrowings",
    "Dividend Paid": "DividendsPaidClassifiedAsFinancingActivities",
}

matric_mapping_industry = {
    "Equity Share Capital": "PaidUpValueOfEquityShareCapital",
    "Net Profit": "ProfitLossForPeriod/ProfitLossForThePeriod",
    "Face Value Of Equity Share Capital": "FaceValueOfEquityShareCapital",
    "sales": "Income",
    "Share Holder’s Equity": "EquityAttributableToOwnersOfParent",
    "Reserve & Surplus": "OtherEquity",
    "Total Debt": "BorrowingsNoncurrent+BorrowingsCurrent",
    "Dividend Paid": "DividendsPaidClassifiedAsFinancingActivities",
}

matric_mapping_others = {
    "Equity Share Capital": "PaidUpValueOfEquityShareCapital",
    "Net Profit": "ProfitLossForPeriod/ProfitLossForThePeriod",
    "Face Value Of Equity Share Capital": "FaceValueOfEquityShareCapital",
    "sales": "Income",
    "Share Holder’s Equity": "ShareholdersFunds",
    "Reserve & Surplus": "ReservesAndSurplus",
    "Total Debt": "LongTermBorrowings+ShortTermBorrowings",
    "Dividend Paid": "DividendsPaidClassifiedAsFinancingActivities",
}

# Symbol -> report type override, used when a filing has no usable XbrlUrl
# (e.g. blank/NaN) and the taxonomy can't be sniffed from it. Ported from
# upload_data_from_files/upload_script.py's special_company_types (found by
# hand while backfilling from CSV exports where XbrlUrl was blank/missing).
special_company_types: dict[str, str] = {
    "IMPAL": "indus",
    "SPIC": "indus",
    "SUNDARMFIN": "Nonbanking",
    "WHEELS": "indus",
    "BHARATRAS": "indus",
    "KARURVYSYA": "banking",
    "SUPRIYA": "indus",
    "COHANCE": "indus",
    "AZAD": "indus",
}


def _xbrl_filename(items: list[dict]) -> str | None:
    for item in items:
        if item.get("Label") == "XbrlUrl":
            value = item.get("Value")
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return None
            value = str(value).strip()
            return value.split("/")[-1] if value else None
    return None


def identify_report_type(
    items: list[dict], symbol: str, rejected_symbols: dict[str, list[str]]
) -> tuple[dict, str] | None:
    """
    Returns (key_mapping, _type) for this filing, or None if the taxonomy
    can't be determined and there's no special-case override for `symbol`
    (in which case a reason is appended to `rejected_symbols[symbol]`).
    """
    filename = _xbrl_filename(items)

    if not filename:
        if symbol in special_company_types:
            _type = special_company_types[symbol]
        else:
            rejected_symbols.setdefault(symbol, []).append(
                "can not figure out file format from XbrlUrl (missing/blank)"
            )
            return None
    elif "IFGI" in filename:
        return matric_mapping_gen_insurance, "GI"
    elif "NonBanking" in filename or "NBFC" in filename:
        return matric_mapping_non_banking, "Nonbanking"
    elif "IFLI" in filename:
        return matric_mapping_life_insurance, "LI"
    elif "IFBanking" in filename or filename.startswith("Banking"):
        return matric_mapping_banking, "banking"
    elif "indas" in filename.lower() or "Main_Ind_As" in filename:
        return matric_mapping_industry, "indus"
    else:
        return matric_mapping_others, "other"

    # symbol matched a special-case override but XbrlUrl couldn't be read
    mapping_by_type = {
        "GI": matric_mapping_gen_insurance,
        "Nonbanking": matric_mapping_non_banking,
        "LI": matric_mapping_life_insurance,
        "banking": matric_mapping_banking,
        "indus": matric_mapping_industry,
        "other": matric_mapping_others,
    }
    return mapping_by_type.get(_type, matric_mapping_others), _type


def _to_number(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _resolve_expression(values_by_label: dict[str, list], expr: str) -> Decimal | None:
    """"A+B" sums terms; "A/B" or "A#B" within a term picks the first present label."""
    expr = (expr or "").strip()
    if not expr:
        return None

    total = Decimal(0)
    found_any = False
    for term in expr.split("+"):
        candidates = [c for c in re.split(r"[/#]", term) if c]
        for label in candidates:
            values = values_by_label.get(label)
            if values:
                number = _to_number(values[0])
                if number is not None:
                    total += number
                    found_any = True
                    break
    return total if found_any else None


def extract_metrics(
    items: list[dict], symbol: str, rejected_symbols: dict[str, list[str]]
) -> dict[str, Decimal]:
    """
    Returns {metric_name: value} for every headline metric whose mapped
    label(s) are present in this filing's items. Metrics whose labels aren't
    present in this particular statement (e.g. balance-sheet metrics when
    `items` is a P&L response) are simply omitted.
    """
    resolved = identify_report_type(items, symbol, rejected_symbols)
    if resolved is None:
        return {}
    key_mapping, _type = resolved

    values_by_label: dict[str, list] = {}
    for item in items:
        label = item.get("Label")
        if label:
            values_by_label.setdefault(label, []).append(item.get("Value"))

    metrics: dict[str, Decimal] = {}
    for metric_name, expr in key_mapping.items():
        value = _resolve_expression(values_by_label, expr)
        if value is not None:
            metrics[metric_name] = value
    return metrics


def _period_end_date(items: list[dict]) -> _dt.date | None:
    values_by_label = {item.get("Label"): item.get("Value") for item in items}
    raw = values_by_label.get("DateOfEndOfReportingPeriod") or values_by_label.get("DateOfEndOfFinancialYear")
    if not raw:
        return None
    try:
        return _dt.datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_period_date(consolidated_items: list[dict] | None, standalone_items: list[dict] | None) -> _dt.date | None:
    """Consolidated's period-end date wins; Standalone's is used only if Consolidated has none."""
    if consolidated_items:
        date = _period_end_date(consolidated_items)
        if date is not None:
            return date
    if standalone_items:
        return _period_end_date(standalone_items)
    return None


def extract_metrics_with_priority(
    consolidated_items: list[dict] | None,
    standalone_items: list[dict] | None,
    symbol: str,
    rejected_symbols: dict[str, list[str]],
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """
    Resolves each headline metric from the Consolidated filing first; any
    metric not found there falls back to the Standalone filing. Returns
    (metric_name -> value, metric_name -> which filing it came from).
    """
    metrics: dict[str, Decimal] = {}
    sources: dict[str, str] = {}

    if consolidated_items:
        for metric_name, value in extract_metrics(consolidated_items, symbol, rejected_symbols).items():
            metrics[metric_name] = value
            sources[metric_name] = "Consolidated"

    if standalone_items:
        for metric_name, value in extract_metrics(standalone_items, symbol, rejected_symbols).items():
            if metric_name not in metrics:
                metrics[metric_name] = value
                sources[metric_name] = "Standalone"

    return metrics, sources


def store_financials(
    db,
    symbol_id: int,
    metrics: dict[str, Decimal],
    sources: dict[str, str],
    date: _dt.date,
    *,
    frequency: str,
    period: str,
    year: int,
) -> int:
    """
    Upserts the given metrics into the `financials` table, one row per
    metric, keyed on (symbol_id, metric_name, date) -- frequency/period/
    report_type/year are overwritten to this run's values on an existing
    match. `report_type` per row is taken from `sources` (Consolidated or
    Standalone, whichever the value was actually resolved from). Returns
    the number of rows written.
    """
    written = 0
    for metric_name, value in metrics.items():
        row = (
            db.query(Financial)
            .filter_by(symbol_id=symbol_id, metric_name=metric_name, date=date)
            .first()
        )
        if row is None:
            row = Financial(symbol_id=symbol_id, date=date, metric_name=metric_name)
            db.add(row)
        row.metric_value = value
        row.frequency = frequency
        row.period = period
        row.report_type = sources.get(metric_name)
        row.year = year
        written += 1
    # Flush so a later store_financials() call in the same session/commit
    # (e.g. a different statement type resolving to the same date) sees
    # these rows as existing rather than re-inserting duplicates -- the
    # session is configured with autoflush=False.
    db.flush()
    return written
