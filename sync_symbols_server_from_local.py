"""
One-off/rerunnable sync: makes the server DB's `symbols` table match the
local DB's for every column EXCEPT `id`, `symbol`, `name`, and `isin` --
those four stay exactly as they are on the server (they're the identity of
the row and what matching is keyed on).

Local and server `symbols` rows are matched by ISIN first, falling back to
the `symbol` code when ISIN is blank -- the same priority migrate_local_to_server.py
and fetch_bhavcopy_symbols.py already use. As of the run that justified writing
this script, every local symbol matched a server row 1:1 (4515 == 4515), so
this only UPDATEs existing server rows; it does not insert or delete any.

sector_id/industry_id can't be copied as raw ids -- local.sectors/industries
and server.sectors/industries are each their own autoincrement sequence, so
the same id can mean a different sector on each side. Instead each local
symbol's sector/industry is resolved to a NAME on the local side, then to
that name's id on the server side, creating the sector/industry row on the
server first if a local name doesn't exist there yet (minimal row: just the
name, plus Industry.sector copied from local for industries).

Usage:
    python sync_symbols_server_from_local.py                 # apply
    python sync_symbols_server_from_local.py --dry-run        # report only, roll back
    python sync_symbols_server_from_local.py --chunk-size 200
"""
from __future__ import annotations

import argparse
import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from db.models.industry import Industry
from db.models.sector import Sector
from db.models.symbol import Symbol
from db.session import SessionLocal as LocalSessionLocal
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 200

# Columns copied from local -> server for every matched symbol. Deliberately
# excludes id/symbol/name/isin (identity, never touched) and sector_id/
# industry_id (handled separately via name-based resolution, not a raw copy).
COPIED_COLUMNS = (
    "exchange",
    "series",
    "archive",
    "listed_date",
    "high_52_week",
    "low_52_week",
    "logo_url",
    "final_status",
)


def make_server_session() -> Session:
    server_url = os.environ.get("SERVER_DATABASE_URL")
    if not server_url:
        raise SystemExit(
            "SERVER_DATABASE_URL is not set. Add it to .env alongside DATABASE_URL."
        )
    engine = create_engine(server_url, pool_pre_ping=True, echo=False)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def resolve_sector_industry_maps(local_db: Session, server_db: Session):
    """
    Returns:
      local_sector_names: {local sector_id: name}
      local_industry_names: {local industry_id: (name, sector_str)}
      server_sector_ids: {name: server sector_id}  -- mutated in place as rows get created
      server_industry_ids: {name: server industry_id}  -- mutated in place as rows get created
    """
    local_sector_names = {s.id: s.name for s in local_db.query(Sector).all()}
    local_industry_names = {i.id: (i.name, i.sector) for i in local_db.query(Industry).all()}
    server_sector_ids = {s.name: s.id for s in server_db.query(Sector).all()}
    server_industry_ids = {i.name: i.id for i in server_db.query(Industry).all()}
    return local_sector_names, local_industry_names, server_sector_ids, server_industry_ids


def ensure_server_sector(server_db: Session, name: str, server_sector_ids: dict, dry_run: bool, prefix: str) -> int:
    sid = server_sector_ids.get(name)
    if sid is not None:
        return sid
    row = Sector(name=name)
    server_db.add(row)
    server_db.flush()
    server_sector_ids[name] = row.id
    logger.info("%screated server sector %r (id=%s)", prefix, name, row.id)
    return row.id


def ensure_server_industry(
    server_db: Session, name: str, sector_str: str | None, server_industry_ids: dict, dry_run: bool, prefix: str
) -> int:
    iid = server_industry_ids.get(name)
    if iid is not None:
        return iid
    row = Industry(name=name, sector=sector_str)
    server_db.add(row)
    server_db.flush()
    server_industry_ids[name] = row.id
    logger.info("%screated server industry %r (id=%s)", prefix, name, row.id)
    return row.id


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main():
    parser = argparse.ArgumentParser(
        description="Update the server `symbols` table's columns (except id/symbol/name/isin) "
        "to match the local DB, resolving sector/industry by name."
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="Compare and report, but roll back instead of committing.")
    args = parser.parse_args()

    configure_logging("sync_symbols_server_from_local")
    prefix = "[DRY RUN] " if args.dry_run else ""

    local_db = LocalSessionLocal()
    server_db = make_server_session()
    try:
        local_sector_names, local_industry_names, server_sector_ids, server_industry_ids = resolve_sector_industry_maps(
            local_db, server_db
        )

        server_syms = server_db.query(Symbol).all()
        by_isin = {s.isin.strip().upper(): s for s in server_syms if s.isin and s.isin.strip()}
        by_symbol = {s.symbol.strip().upper(): s for s in server_syms if s.symbol and s.symbol.strip()}

        local_syms = local_db.query(Symbol).order_by(Symbol.id.asc()).all()

        stats = {"matched_updated": 0, "matched_unchanged": 0, "unmatched_skipped": 0}
        unmatched_symbols = []

        for batch_num, batch in enumerate(chunked(local_syms, args.chunk_size), start=1):
            batch_changed = 0
            for ls in batch:
                isin = (ls.isin or "").strip().upper()
                code = (ls.symbol or "").strip().upper()
                srow = by_isin.get(isin) if isin else None
                if srow is None and code:
                    srow = by_symbol.get(code)
                if srow is None:
                    stats["unmatched_skipped"] += 1
                    unmatched_symbols.append(ls.symbol)
                    continue

                changed_fields = []
                for field in COPIED_COLUMNS:
                    local_value = getattr(ls, field)
                    if getattr(srow, field) != local_value:
                        setattr(srow, field, local_value)
                        changed_fields.append(field)

                local_sector_name = local_sector_names.get(ls.sector_id) if ls.sector_id else None
                target_sector_id = None
                if local_sector_name:
                    target_sector_id = ensure_server_sector(server_db, local_sector_name, server_sector_ids, args.dry_run, prefix)
                if srow.sector_id != target_sector_id:
                    srow.sector_id = target_sector_id
                    changed_fields.append("sector_id")

                local_industry = local_industry_names.get(ls.industry_id) if ls.industry_id else None
                target_industry_id = None
                if local_industry:
                    ind_name, ind_sector_str = local_industry
                    target_industry_id = ensure_server_industry(
                        server_db, ind_name, ind_sector_str, server_industry_ids, args.dry_run, prefix
                    )
                if srow.industry_id != target_industry_id:
                    srow.industry_id = target_industry_id
                    changed_fields.append("industry_id")

                if changed_fields:
                    stats["matched_updated"] += 1
                    batch_changed += 1
                else:
                    stats["matched_unchanged"] += 1

            if args.dry_run:
                server_db.flush()
            else:
                server_db.commit()
            logger.info(
                "%sbatch %s (%s symbols): %s updated this batch",
                prefix, batch_num, len(batch), batch_changed,
            )

        logger.info(
            "%sdone: %s updated, %s already matched, %s local symbols had no server match (skipped)",
            prefix, stats["matched_updated"], stats["matched_unchanged"], stats["unmatched_skipped"],
        )
        if unmatched_symbols:
            logger.warning("%sunmatched local symbols (no server row by ISIN/symbol): %s", prefix, unmatched_symbols[:50])

        if args.dry_run:
            server_db.rollback()
            logger.info("[DRY RUN] rolled back everything -- nothing was written to the server.")
    finally:
        local_db.close()
        server_db.close()


if __name__ == "__main__":
    main()
