"""
One-off/rerunnable migration: copies `symbols`, `financials`, `pnl_financials`,
`balance_sheet_financials`, `cashflow_financials`, `price_snapshots`, and
`price_snapshots_new` from the local DB (DATABASE_URL) to a separate server DB
(SERVER_DATABASE_URL, a new .env var -- add it alongside DATABASE_URL before
running this).

SYMBOL_ID RECONCILIATION: local.symbols.id and server.symbols.id are each
independent autoincrement sequences -- the same real-world symbol can have
a different id on each side. This script never assumes they match: for
every local symbol it looks up the corresponding server row by ISIN first
(falling back to the `symbol` code when ISIN is blank -- same matching
priority fetch_bhavcopy_symbols.py's upsert_symbols() already uses), and
builds a {local_id: server_id} map. Every financials/pnl_financials/
balance_sheet_financials/cashflow_financials row is written with the
REMAPPED server symbol_id, never the local one. A local symbol not found on
the server is inserted there first (industry_id/sector_id are left NULL on
the new row -- sectors/industries aren't part of this migration's scope, so
their local ids would be meaningless on the server).

SKIP RULE (never overwrites anything already on the server):
  - symbols: matched by ISIN/symbol -> use the existing server row/id as-is,
    don't touch its other columns.
  - financials: matched by (metric_name, date, symbol_id, frequency, period)
    -- the exact key given for this migration.
  - pnl_financials / balance_sheet_financials / cashflow_financials: matched
    by (symbol_id, statement_type, frequency, date, metric_name) -- the same
    key these 3 tables' own UniqueConstraint already enforces, and the one
    upsert_detailed_metric()/the ETL script's ON CONFLICT target already use
    elsewhere in this codebase.
  - price_snapshots / price_snapshots_new: matched by (symbol_id, date) --
    the same key insert_price_snapshots() (fetch_price_snapshots.py) already
    treats as this table's natural uniqueness (one row per symbol per day).
A row whose key already exists on the server is skipped entirely (no
update); only genuinely missing rows are inserted. This also makes the
script resumable -- rerunning after a partial/interrupted run just inserts
whatever is still missing.

Usage:
    python migrate_local_to_server.py                     # migrate everything
    python migrate_local_to_server.py --dry-run            # fetch/compare, roll back instead of commit
    python migrate_local_to_server.py --tables symbols,financials
    python migrate_local_to_server.py --tables price_snapshots,price_snapshots_new --chunk-size 15
    python migrate_local_to_server.py --chunk-size 25
"""
from __future__ import annotations

import argparse
import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.financials import Financial
from db.models.pnl_financials import PnlFinancial
from db.models.price_snapshots import PriceSnapshot
from db.models.price_snapshots_new import PriceSnapshotNew
from db.models.symbol import Symbol
from db.session import SessionLocal as LocalSessionLocal
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 50
ALL_TABLES = ("symbols", "financials", "pnl", "balance_sheet", "cashflow", "price_snapshots", "price_snapshots_new")

DETAILED_TABLE_CONFIG = {
    "pnl": (PnlFinancial, "pnl_financials"),
    "balance_sheet": (BalanceSheetFinancial, "balance_sheet_financials"),
    "cashflow": (CashflowFinancial, "cashflow_financials"),
}

PRICE_SNAPSHOT_TABLE_CONFIG = {
    "price_snapshots": (PriceSnapshot, "price_snapshots"),
    "price_snapshots_new": (PriceSnapshotNew, "price_snapshots_new"),
}


def make_server_session() -> Session:
    server_url = os.environ.get("SERVER_DATABASE_URL")
    if not server_url:
        raise SystemExit(
            "SERVER_DATABASE_URL is not set. Add it to .env alongside DATABASE_URL "
            "(same postgresql+psycopg2://... format), pointing at the server DB."
        )
    engine = create_engine(server_url, pool_pre_ping=True, echo=False)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def migrate_symbols(local_db: Session, server_db: Session, dry_run: bool) -> tuple[dict[int, int], dict]:
    """
    Returns ({local_symbol_id: server_symbol_id}, stats). Every local symbol
    gets an entry in the map -- either an existing server row (matched by
    ISIN, falling back to `symbol`) left untouched, or a freshly inserted one.
    """
    stats = {"matched_existing": 0, "inserted": 0}
    prefix = "[DRY RUN] " if dry_run else ""

    server_symbols = server_db.query(Symbol).all()
    by_isin = {s.isin.strip().upper(): s for s in server_symbols if s.isin and s.isin.strip()}
    by_symbol = {s.symbol.strip().upper(): s for s in server_symbols if s.symbol and s.symbol.strip()}

    symbol_id_map: dict[int, int] = {}
    local_symbols = local_db.query(Symbol).order_by(Symbol.id.asc()).all()

    for local_sym in local_symbols:
        isin = (local_sym.isin or "").strip().upper()
        code = (local_sym.symbol or "").strip().upper()

        server_row = by_isin.get(isin) if isin else None
        if server_row is None and code:
            server_row = by_symbol.get(code)

        if server_row is not None:
            symbol_id_map[local_sym.id] = server_row.id
            stats["matched_existing"] += 1
            continue

        new_row = Symbol(
            symbol=local_sym.symbol,
            name=local_sym.name,
            exchange=local_sym.exchange,
            isin=local_sym.isin,
            logo_url=local_sym.logo_url,
            series=local_sym.series,
            archive=local_sym.archive,
            listed_date=local_sym.listed_date,
            high_52_week=local_sym.high_52_week,
            low_52_week=local_sym.low_52_week,
            final_status=local_sym.final_status,
            industry_id=None,
            sector_id=None,
        )
        server_db.add(new_row)
        server_db.flush()  # need new_row.id before it can go in the map
        symbol_id_map[local_sym.id] = new_row.id
        if isin:
            by_isin[isin] = new_row
        if code:
            by_symbol[code] = new_row
        stats["inserted"] += 1
        logger.info("%snew symbol on server: %r (local id=%s -> server id=%s)", prefix, local_sym.symbol, local_sym.id, new_row.id)

    # Dry-run: flush only (keeps new rows visible within this open transaction
    # for the financial-table steps that follow, without persisting anything)
    # -- the one rollback for the whole run happens once, at the end of main().
    # Real run: commit for real so these rows are valid FK targets going forward.
    if dry_run:
        server_db.flush()
    else:
        server_db.commit()

    logger.info(
        "%ssymbols: %s matched existing (untouched), %s inserted",
        prefix, stats["matched_existing"], stats["inserted"],
    )
    return symbol_id_map, stats


def migrate_financials(local_db: Session, server_db: Session, symbol_id_map: dict[int, int], chunk_size: int, dry_run: bool) -> dict:
    """Match key: (metric_name, date, symbol_id, frequency, period) -- as specified for this migration."""
    stats = {"checked": 0, "inserted": 0, "skipped_existing": 0}
    prefix = "[DRY RUN] " if dry_run else ""
    local_ids = list(symbol_id_map.keys())

    for batch_num, local_batch in enumerate(chunked(local_ids, chunk_size), start=1):
        server_batch = [symbol_id_map[lid] for lid in local_batch]

        local_rows = local_db.query(Financial).filter(Financial.symbol_id.in_(local_batch)).all()
        existing_keys = {
            (sid, metric_name, date, frequency, period)
            for sid, metric_name, date, frequency, period in server_db.query(
                Financial.symbol_id, Financial.metric_name, Financial.date, Financial.frequency, Financial.period
            ).filter(Financial.symbol_id.in_(server_batch)).all()
        }

        to_insert = []
        for row in local_rows:
            stats["checked"] += 1
            server_symbol_id = symbol_id_map[row.symbol_id]
            key = (server_symbol_id, row.metric_name, row.date, row.frequency, row.period)
            if key in existing_keys:
                stats["skipped_existing"] += 1
                continue
            existing_keys.add(key)
            to_insert.append(
                Financial(
                    symbol_id=server_symbol_id,
                    date=row.date,
                    frequency=row.frequency,
                    period=row.period,
                    report_type=row.report_type,
                    metric_name=row.metric_name,
                    metric_value=row.metric_value,
                    year=row.year,
                )
            )

        if to_insert:
            server_db.bulk_save_objects(to_insert)
        if dry_run:
            server_db.flush()
        else:
            server_db.commit()

        logger.info(
            "%sfinancials batch %s (%s symbol(s)): %s checked, %s inserted, %s already present",
            prefix, batch_num, len(local_batch), len(local_rows), len(to_insert), len(local_rows) - len(to_insert),
        )

    stats["inserted"] = stats["checked"] - stats["skipped_existing"]
    logger.info("%sfinancials total: %s checked, %s inserted, %s already present", prefix, stats["checked"], stats["inserted"], stats["skipped_existing"])
    return stats


def migrate_detailed_table(
    local_db: Session,
    server_db: Session,
    model,
    label: str,
    symbol_id_map: dict[int, int],
    chunk_size: int,
    dry_run: bool,
) -> dict:
    """Match key: (symbol_id, statement_type, frequency, date, metric_name) -- this table's own UniqueConstraint."""
    stats = {"checked": 0, "inserted": 0, "skipped_existing": 0}
    prefix = "[DRY RUN] " if dry_run else ""
    local_ids = list(symbol_id_map.keys())

    for batch_num, local_batch in enumerate(chunked(local_ids, chunk_size), start=1):
        server_batch = [symbol_id_map[lid] for lid in local_batch]

        local_rows = local_db.query(model).filter(model.symbol_id.in_(local_batch)).all()
        existing_keys = {
            (sid, statement_type, frequency, date, metric_name)
            for sid, statement_type, frequency, date, metric_name in server_db.query(
                model.symbol_id, model.statement_type, model.frequency, model.date, model.metric_name
            ).filter(model.symbol_id.in_(server_batch)).all()
        }

        to_insert = []
        for row in local_rows:
            stats["checked"] += 1
            server_symbol_id = symbol_id_map[row.symbol_id]
            key = (server_symbol_id, row.statement_type, row.frequency, row.date, row.metric_name)
            if key in existing_keys:
                stats["skipped_existing"] += 1
                continue
            existing_keys.add(key)
            to_insert.append(
                model(
                    symbol_id=server_symbol_id,
                    statement_type=row.statement_type,
                    date=row.date,
                    frequency=row.frequency,
                    period=row.period,
                    metric_name=row.metric_name,
                    metric_value=row.metric_value,
                    year=row.year,
                )
            )

        if to_insert:
            server_db.bulk_save_objects(to_insert)
        if dry_run:
            server_db.flush()
        else:
            server_db.commit()

        logger.info(
            "%s%s batch %s (%s symbol(s)): %s checked, %s inserted, %s already present",
            prefix, label, batch_num, len(local_batch), len(local_rows), len(to_insert), len(local_rows) - len(to_insert),
        )

    stats["inserted"] = stats["checked"] - stats["skipped_existing"]
    logger.info("%s%s total: %s checked, %s inserted, %s already present", prefix, label, stats["checked"], stats["inserted"], stats["skipped_existing"])
    return stats


def migrate_price_snapshots(
    local_db: Session,
    server_db: Session,
    model,
    label: str,
    symbol_id_map: dict[int, int],
    chunk_size: int,
    dry_run: bool,
) -> dict:
    """Match key: (symbol_id, date) -- one row per symbol per day, same as insert_price_snapshots()."""
    stats = {"checked": 0, "inserted": 0, "skipped_existing": 0}
    prefix = "[DRY RUN] " if dry_run else ""
    local_ids = list(symbol_id_map.keys())

    for batch_num, local_batch in enumerate(chunked(local_ids, chunk_size), start=1):
        server_batch = [symbol_id_map[lid] for lid in local_batch]

        local_rows = local_db.query(model).filter(model.symbol_id.in_(local_batch)).all()
        existing_keys = {
            (sid, d) for sid, d in server_db.query(model.symbol_id, model.date).filter(model.symbol_id.in_(server_batch)).all()
        }

        to_insert = []
        for row in local_rows:
            stats["checked"] += 1
            server_symbol_id = symbol_id_map[row.symbol_id]
            key = (server_symbol_id, row.date)
            if key in existing_keys:
                stats["skipped_existing"] += 1
                continue
            existing_keys.add(key)
            to_insert.append(
                model(
                    symbol_id=server_symbol_id,
                    date=row.date,
                    open_price=row.open_price,
                    close_price=row.close_price,
                    high=row.high,
                    low=row.low,
                    volume=row.volume,
                )
            )

        if to_insert:
            server_db.bulk_save_objects(to_insert)
        if dry_run:
            server_db.flush()
        else:
            server_db.commit()

        logger.info(
            "%s%s batch %s (%s symbol(s)): %s checked, %s inserted, %s already present",
            prefix, label, batch_num, len(local_batch), len(local_rows), len(to_insert), len(local_rows) - len(to_insert),
        )

    stats["inserted"] = stats["checked"] - stats["skipped_existing"]
    logger.info("%s%s total: %s checked, %s inserted, %s already present", prefix, label, stats["checked"], stats["inserted"], stats["skipped_existing"])
    return stats


def main():
    parser = argparse.ArgumentParser(description="Migrate symbols/financials/pnl/balance_sheet/cashflow from local DB to server DB.")
    parser.add_argument("--tables", default=",".join(ALL_TABLES), help=f"Comma-separated subset of {ALL_TABLES} (default: all).")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Symbols per batch when migrating the financial tables.")
    parser.add_argument("--dry-run", action="store_true", help="Compare and report, but roll back instead of committing to the server.")
    args = parser.parse_args()

    configure_logging("migrate_local_to_server")
    requested = {t.strip() for t in args.tables.split(",") if t.strip()}
    unknown = requested - set(ALL_TABLES)
    if unknown:
        raise SystemExit(f"Unknown --tables value(s): {sorted(unknown)}; choose from {ALL_TABLES}")

    local_db = LocalSessionLocal()
    server_db = make_server_session()
    try:
        # symbols always runs first (even if not explicitly requested) when any
        # financial table is requested -- the id map it builds is required by all of them.
        needs_symbol_map = requested & {
            "financials", "pnl", "balance_sheet", "cashflow", "price_snapshots", "price_snapshots_new",
        }
        if "symbols" in requested or needs_symbol_map:
            symbol_id_map, _ = migrate_symbols(local_db, server_db, args.dry_run)
        else:
            symbol_id_map = {}

        if "financials" in requested:
            migrate_financials(local_db, server_db, symbol_id_map, args.chunk_size, args.dry_run)

        for key, (model, label) in PRICE_SNAPSHOT_TABLE_CONFIG.items():
            if key in requested:
                migrate_price_snapshots(local_db, server_db, model, label, symbol_id_map, args.chunk_size, args.dry_run)

        for key, (model, label) in DETAILED_TABLE_CONFIG.items():
            if key in requested:
                migrate_detailed_table(local_db, server_db, model, label, symbol_id_map, args.chunk_size, args.dry_run)

        # Every step above only flushed (not committed) in dry-run mode, so new
        # symbol rows stayed visible -- as valid FK targets -- to the financial-
        # table steps that followed, without ever persisting. This one rollback
        # discards the entire run at once now that everything's been compared.
        if args.dry_run:
            server_db.rollback()
            logger.info("[DRY RUN] rolled back everything -- nothing was written to the server.")
    finally:
        local_db.close()
        server_db.close()


if __name__ == "__main__":
    main()
