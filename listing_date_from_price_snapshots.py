"""
Dry-run report: estimate a listing date for ACCEPTED symbols that don't
already have `listed_date` set, using the earliest `date` present in
`price_snapshots` for that symbol. Symbols that already have a
`listed_date` are skipped entirely -- nothing to do for those.

Read-only -- this script never writes to the DB, it only produces a JSON
report for review.

Output is a JSON array, one object per ACCEPTED symbol with no existing
`listed_date`:
    {symbol, isin, oldest_price_snapshot_date, price_snapshot_row_count}

Usage:
    python listing_date_from_price_snapshots.py
    python listing_date_from_price_snapshots.py --limit 50
    python listing_date_from_price_snapshots.py --output out.json
"""

import argparse
import json

from sqlalchemy import func

from db.session import SessionLocal
from db.models.symbol import Symbol
from db.models.price_snapshots import PriceSnapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="listing_date_dry_run_report.json")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N ACCEPTED symbols")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        all_accepted = db.query(Symbol).filter(Symbol.final_status == "ACCEPTED").all()
        symbols = [s for s in all_accepted if s.listed_date is None]
        print(f"ACCEPTED symbols in DB: {len(all_accepted)}")
        print(f"ACCEPTED symbols with NO existing listed_date (need a guess): {len(symbols)}")
        print(f"ACCEPTED symbols that already have listed_date (skipped, nothing to do): {len(all_accepted) - len(symbols)}")

        if args.limit:
            symbols = symbols[: args.limit]

        oldest_rows = (
            db.query(
                PriceSnapshot.symbol_id,
                func.min(PriceSnapshot.date).label("oldest_date"),
                func.count(PriceSnapshot.id).label("row_count"),
            )
            .group_by(PriceSnapshot.symbol_id)
            .all()
        )
        oldest_by_symbol_id = {r.symbol_id: (r.oldest_date, r.row_count) for r in oldest_rows}

        rows_out = []
        missing_snapshot_count = 0

        for s in symbols:
            oldest_date, row_count = oldest_by_symbol_id.get(s.id, (None, 0))
            if oldest_date is None:
                missing_snapshot_count += 1

            rows_out.append({
                "symbol": s.symbol,
                "isin": s.isin,
                "oldest_price_snapshot_date": oldest_date,
                "price_snapshot_row_count": row_count,
            })

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(rows_out, f, indent=2, ensure_ascii=False, default=str)

        print("[DRY RUN] Read-only report -- no DB writes made.")
        print(f"Wrote {len(rows_out)} entries to {args.output}")
        print(f"  symbols with NO price_snapshots rows at all: {missing_snapshot_count}/{len(rows_out)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
