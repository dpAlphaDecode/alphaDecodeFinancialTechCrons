"""
Computes "Earning Per Share" and "Outstanding Share" per quarterly
(symbol_id, year, period, frequency, date, report_type) group in
`financials`, from that same group's "Equity Share Capital", "Face Value
Of Equity Share Capital", and "Net Profit" rows, and stores them back into
`financials` under those exact metric_name values.

Formula (frequency="quarterly" only -- annual/legacy "yearly" rows aren't
touched):
    face_value in (0, None), or (net_profit is None AND equity is None):
        eps = 0; outstanding_shares = 0
    net_profit is None:
        eps = 0; outstanding_shares = equity / face_value
    equity is None:
        eps = (net_profit / 1e7) / face_value; outstanding_shares = 0
    else:
        eps = (net_profit / 1e7) / face_value; outstanding_shares = equity / face_value

Never overwrites: for a given (symbol_id, date), if a row for
"Earning Per Share" (respectively "Outstanding Share") already exists in
`financials` -- regardless of its value -- that one is left alone; only a
genuinely missing one gets inserted. The two metrics are checked
independently, so a quarter missing just one of the two still gets that
one filled in.

This is a reporting/dry-run tool by default: it only prints what would be
inserted. Pass --apply to actually commit.

Usage:
    python compute_eps_and_outstanding_shares.py            # dry run
    python compute_eps_and_outstanding_shares.py --apply
"""
from __future__ import annotations

import argparse
import logging
from decimal import Decimal

from dotenv import load_dotenv

from db.models.financials import Financial
from db.session import SessionLocal
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

FREQUENCY = "quarterly"
SOURCE_METRICS = ("Equity Share Capital", "Face Value Of Equity Share Capital", "Net Profit")
EPS_METRIC = "Earning Per Share"
OUTSTANDING_METRIC = "Outstanding Share"
CRORE = Decimal(10_000_000)


def compute_eps_and_outstanding(equity, face_value, net_profit) -> tuple[Decimal, Decimal]:
    """Mirrors the exact logic specified: eps/outstanding default to 0 wherever an input needed for them is missing."""
    if (face_value in (0, None)) or (net_profit is None and equity is None):
        return Decimal(0), Decimal(0)
    if net_profit is None:
        return Decimal(0), equity / face_value
    if equity is None:
        return (net_profit / CRORE) / face_value, Decimal(0)
    return (net_profit / CRORE) / face_value, equity / face_value


def load_source_groups(db) -> dict[tuple, dict[str, Decimal]]:
    """{(symbol_id, year, period, frequency, date, report_type): {metric_name: value}} for quarterly source rows."""
    rows = (
        db.query(
            Financial.symbol_id,
            Financial.year,
            Financial.period,
            Financial.frequency,
            Financial.date,
            Financial.report_type,
            Financial.metric_name,
            Financial.metric_value,
        )
        .filter(Financial.frequency == FREQUENCY, Financial.metric_name.in_(SOURCE_METRICS))
        .all()
    )
    grouped: dict[tuple, dict[str, Decimal]] = {}
    for row in rows:
        key = (row.symbol_id, row.year, row.period, row.frequency, row.date, row.report_type)
        grouped.setdefault(key, {})[row.metric_name] = row.metric_value
    return grouped


def load_existing_targets(db) -> set[tuple[int, object, str]]:
    """{(symbol_id, date, metric_name)} already present for EPS/Outstanding Share, quarterly -- never overwritten."""
    rows = (
        db.query(Financial.symbol_id, Financial.date, Financial.metric_name)
        .filter(Financial.frequency == FREQUENCY, Financial.metric_name.in_((EPS_METRIC, OUTSTANDING_METRIC)))
        .all()
    )
    return {(r.symbol_id, r.date, r.metric_name) for r in rows}


def run(dry_run: bool = True, db=None) -> dict:
    """`db` lets a caller pass in a session it already owns; when omitted, this opens and closes its own."""
    stats = {"groups": 0, "eps_inserted": 0, "eps_skipped_existing": 0, "outstanding_inserted": 0, "outstanding_skipped_existing": 0}
    prefix = "[DRY RUN] " if dry_run else ""

    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        grouped = load_source_groups(db)
        existing_targets = load_existing_targets(db)
        stats["groups"] = len(grouped)
        logger.info("%s quarterly (symbol, year, period, report_type) group(s) with source metrics found.", len(grouped))

        for (symbol_id, year, period, frequency, date, report_type), metrics in grouped.items():
            equity = metrics.get("Equity Share Capital")
            face_value = metrics.get("Face Value Of Equity Share Capital")
            net_profit = metrics.get("Net Profit")
            eps, outstanding = compute_eps_and_outstanding(equity, face_value, net_profit)

            for metric_name, value, stat_inserted, stat_skipped in (
                (EPS_METRIC, eps, "eps_inserted", "eps_skipped_existing"),
                (OUTSTANDING_METRIC, outstanding, "outstanding_inserted", "outstanding_skipped_existing"),
            ):
                if (symbol_id, date, metric_name) in existing_targets:
                    stats[stat_skipped] += 1
                    continue

                verb = "would insert" if dry_run else "inserting"
                logger.info(
                    "%ssymbol_id=%s date=%s (%s %s): financials.%r MISSING -> %s %s",
                    prefix, symbol_id, date, year, period, metric_name, verb, value,
                )
                if not dry_run:
                    db.add(
                        Financial(
                            symbol_id=symbol_id,
                            date=date,
                            metric_name=metric_name,
                            metric_value=value,
                            frequency=frequency,
                            period=period,
                            year=year,
                            report_type=report_type,
                        )
                    )
                    existing_targets.add((symbol_id, date, metric_name))
                stats[stat_inserted] += 1

        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        if owns_db:
            db.close()

    logger.info(
        "%sSummary: %s group(s) -- EPS: %s would insert, %s already present; "
        "Outstanding Share: %s would insert, %s already present.",
        prefix, stats["groups"], stats["eps_inserted"], stats["eps_skipped_existing"],
        stats["outstanding_inserted"], stats["outstanding_skipped_existing"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Compute and store Earning Per Share / Outstanding Share in `financials` (quarterly), "
        "from Equity Share Capital / Face Value / Net Profit. Never overwrites an existing value."
    )
    parser.add_argument("--apply", action="store_true", help="Commit for real (default: dry run, prints only).")
    args = parser.parse_args()
    configure_logging("compute_eps_and_outstanding_shares")
    run(dry_run=not args.apply)


if __name__ == "__main__":
    main()
