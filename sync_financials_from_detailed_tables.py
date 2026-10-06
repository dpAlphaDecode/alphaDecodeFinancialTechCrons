"""
Copies values for the handful of metrics that reference the same (or an
equivalent) underlying GDFL API label in both `financials` and one of the
detailed tables (pnl_financials/balance_sheet_financials/cashflow_financials)
into `financials` -- reconciling it against the detailed tables instead of
(or in addition to) financial_metrics.py's own independent
matric_mapping_*-based extraction straight from the API.

Metric pairs (financials.metric_name <- detailed_table.metric_name) -- the
only ones confirmed to reference the same/equivalent API label on both
sides (see financial_metrics.py's matric_mapping_* dicts vs
Detailed_page_keys.xlsx's per-sector columns):
    "Net Profit"           <- pnl_financials."Net Profit"
    "Equity Share Capital" <- balance_sheet_financials."Equity Share Capital"
    "Reserve & Surplus"    <- balance_sheet_financials."Reserves & Surplus"
    "Dividend Paid"        <- cashflow_financials."Dividends Paid"
("Equity Share Capital" is a partial exception -- see CAVEAT below.)

Per symbol+date, a detailed table can hold up to two rows for the same
metric: one with statement_type="consolidated", one "standalone" --
upsert_detailed_metric()'s lookup key includes statement_type, so a metric
fetched once when only Standalone was available and again later when
Consolidated became available leaves BOTH rows rather than overwriting one.
This script prefers "consolidated", falling back to "standalone" only when
no consolidated row exists for that (symbol_id, date) -- the same priority
financial_metrics.py's own extract_metrics_with_priority() already applies
elsewhere. If both exist AND disagree in value, that's a genuine conflict
(not just a missing-vs-present difference) and is reported, not guessed at.

CAVEAT -- excluded legacy data: all three detailed tables also contain a
large number of rows with frequency="yearly" (389k-1.9M rows per table) --
distinct from the current pipeline's "annual"/"quarterly" convention (see
fetch_historical_financial_stock_details.py's store_detailed_financials_for_symbol()).
Spot-checking shows these "yearly" rows carry mismatched period/year values
(e.g. period="Q4" on a row that should be a full-year figure) and are
presumably a leftover from an older/different ingestion path (possibly
upload_data_from_files/upload_script.py). This script only reads
frequency IN ("annual", "quarterly") rows -- "yearly" rows are excluded
entirely and reported as a count so the gap is visible, not silently
copied in with potentially wrong values.

`financials` is matched/upserted keyed on (symbol_id, metric_name, date) --
the same key financial_metrics.py's store_financials() uses.

This is a reporting/dry-run tool: it only prints what would change. Pass
--apply to actually commit (still prints the same report first).

Usage:
    python sync_financials_from_detailed_tables.py            # dry run (default)
    python sync_financials_from_detailed_tables.py --apply     # commit for real
"""
from __future__ import annotations

import argparse
import logging
from decimal import Decimal

from dotenv import load_dotenv

from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.financials import Financial
from db.models.pnl_financials import PnlFinancial
from db.session import SessionLocal
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

CURRENT_FREQUENCIES = ("annual", "quarterly")

# (source model, source metric_name, target financials.metric_name)
METRIC_MAPPINGS = [
    (PnlFinancial, "Net Profit", "Net Profit"),
    (BalanceSheetFinancial, "Equity Share Capital", "Equity Share Capital"),
    (BalanceSheetFinancial, "Reserves & Surplus", "Reserve & Surplus"),
    (CashflowFinancial, "Dividends Paid", "Dividend Paid"),
]


def _values_equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return Decimal(a) == Decimal(b)


def pick_preferred_row(rows: list):
    """
    Prefers a "consolidated" row over "standalone"; returns (row, is_conflict).
    is_conflict=True means two rows at the SAME preferred statement_type
    disagree in value -- caller should skip rather than guess, and `row`
    is then None.
    """
    consolidated = [r for r in rows if r.statement_type == "consolidated"]
    standalone = [r for r in rows if r.statement_type == "standalone"]
    candidates = consolidated or standalone

    distinct_values = {c.metric_value for c in candidates}
    if len(distinct_values) > 1:
        return None, True
    return candidates[0], False


def sync_metric(db, model, source_metric_name: str, target_metric_name: str, dry_run: bool) -> dict:
    """
    financials.date alone does NOT uniquely identify "annual" vs "quarterly"
    -- a fiscal year's Q4 quarter-end date (e.g. 31-Mar) is the exact same
    calendar date as that year's annual period-end date. So a detailed
    table can legitimately hold BOTH an "annual" row and a "quarterly" row
    for the same (symbol_id, date) with genuinely different values (full-
    year figure vs Q4-only figure) -- that's not a data conflict, and must
    not be grouped together as if it were one. Grouping is therefore keyed
    on (symbol_id, date, frequency) first; only rows sharing frequency too
    (i.e. actually competing to be THE value for that exact slot) are
    checked against each other for a real conflict.

    `financials` itself has no frequency column in its upsert key (just
    symbol_id/metric_name/date -- see store_financials()), so when a given
    (symbol_id, date) has both an annual and a quarterly candidate, this
    matches whichever frequency the EXISTING financials row already has
    (to avoid flipping it between the two), falling back to preferring
    "annual" when there's no existing row to match.
    """
    stats = {
        "candidates": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "conflicts": 0,
        "legacy_yearly_rows_excluded": 0,
    }
    prefix = "[DRY RUN] " if dry_run else ""

    all_rows = db.query(model).filter(model.metric_name == source_metric_name).all()
    stats["legacy_yearly_rows_excluded"] = sum(1 for r in all_rows if r.frequency not in CURRENT_FREQUENCIES)

    by_symbol_date: dict[tuple[int, object], dict[str, list]] = {}
    for row in all_rows:
        if row.frequency not in CURRENT_FREQUENCIES:
            continue
        by_symbol_date.setdefault((row.symbol_id, row.date), {}).setdefault(row.frequency, []).append(row)

    stats["candidates"] = len(by_symbol_date)

    existing_financials = {
        (row.symbol_id, row.date): row
        for row in db.query(Financial).filter(Financial.metric_name == target_metric_name).all()
    }

    for (symbol_id, date), freq_groups in by_symbol_date.items():
        existing = existing_financials.get((symbol_id, date))
        if existing is not None and existing.frequency in freq_groups:
            chosen_frequency = existing.frequency
        elif "annual" in freq_groups:
            chosen_frequency = "annual"
        else:
            chosen_frequency = next(iter(freq_groups))

        preferred, is_conflict = pick_preferred_row(freq_groups[chosen_frequency])
        if is_conflict:
            logger.warning(
                "%sCONFLICT: symbol_id=%s date=%s frequency=%r %s.%r: multiple same-priority rows disagree (%s); skipping.",
                prefix, symbol_id, date, chosen_frequency, model.__tablename__, source_metric_name,
                [(r.statement_type, r.metric_value) for r in freq_groups[chosen_frequency]],
            )
            stats["conflicts"] += 1
            continue

        value = preferred.metric_value
        report_type = preferred.statement_type.capitalize()  # "consolidated"/"standalone" -> financials' "Consolidated"/"Standalone"

        if existing is None:
            verb = "would insert" if dry_run else "inserting"
            logger.info(
                "%ssymbol_id=%s date=%s: financials.%r MISSING -> %s %s (%s, %s/%s, FY%s)",
                prefix, symbol_id, date, target_metric_name, verb, value,
                report_type, preferred.frequency, preferred.period or "", preferred.year,
            )
            if not dry_run:
                db.add(
                    Financial(
                        symbol_id=symbol_id,
                        date=date,
                        metric_name=target_metric_name,
                        metric_value=value,
                        frequency=preferred.frequency,
                        period=preferred.period,
                        year=preferred.year,
                        report_type=report_type,
                    )
                )
            stats["inserted"] += 1
        elif not _values_equal(existing.metric_value, value):
            logger.info(
                "%ssymbol_id=%s date=%s: financials.%r %s -> %s (%s)",
                prefix, symbol_id, date, target_metric_name, existing.metric_value, value, report_type,
            )
            if not dry_run:
                existing.metric_value = value
                existing.frequency = preferred.frequency
                existing.period = preferred.period
                existing.year = preferred.year
                existing.report_type = report_type
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1

    return stats


def run(dry_run: bool = True) -> dict:
    db = SessionLocal()
    overall = {}
    try:
        for model, source_metric_name, target_metric_name in METRIC_MAPPINGS:
            key = f"{model.__tablename__}.{source_metric_name} -> financials.{target_metric_name}"
            logger.info("=== %s ===", key)
            overall[key] = sync_metric(db, model, source_metric_name, target_metric_name, dry_run=dry_run)

        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        db.close()

    prefix = "[DRY RUN] " if dry_run else ""
    logger.info("%s===== Summary =====", prefix)
    for key, stats in overall.items():
        logger.info(
            "%s%s: %s candidate (symbol,date) group(s) -- %s would insert, %s would update, "
            "%s unchanged, %s conflict(s) skipped, %s legacy frequency='yearly' row(s) excluded.",
            prefix, key, stats["candidates"], stats["inserted"], stats["updated"],
            stats["unchanged"], stats["conflicts"], stats["legacy_yearly_rows_excluded"],
        )
    return overall


def main():
    parser = argparse.ArgumentParser(
        description="Copy Net Profit/Equity Share Capital/Reserve & Surplus/Dividend Paid from the "
        "detailed tables into `financials` (Consolidated preferred, Standalone fallback)."
    )
    parser.add_argument("--apply", action="store_true", help="Commit for real (default: dry run, prints only).")
    args = parser.parse_args()
    configure_logging("sync_financials_from_detailed_tables")
    run(dry_run=not args.apply)


if __name__ == "__main__":
    main()
