"""
Apply the oldest `price_snapshots` date as `listed_date` for ACCEPTED
symbols that currently have no `listed_date` set.

Without --apply: just prints the update plan (count + sample rows), makes
no DB changes. With --apply: runs the actual UPDATE against `symbols`.

Only touches symbols where listed_date IS NULL -- symbols that already
have a listed_date are never modified.

Usage:
    python apply_listed_date_from_price_snapshots.py            # show plan only
    python apply_listed_date_from_price_snapshots.py --apply    # actually write
"""

import argparse

from sqlalchemy import func

from db.session import SessionLocal
from db.models.symbol import Symbol
from db.models.price_snapshots import PriceSnapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write to the DB. Without this, only prints the plan.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        symbols = (
            db.query(Symbol)
            .filter(Symbol.final_status == "ACCEPTED", Symbol.listed_date.is_(None))
            .all()
        )

        oldest_rows = (
            db.query(
                PriceSnapshot.symbol_id,
                func.min(PriceSnapshot.date).label("oldest_date"),
            )
            .group_by(PriceSnapshot.symbol_id)
            .all()
        )
        oldest_by_symbol_id = {r.symbol_id: r.oldest_date for r in oldest_rows}

        plan = []
        for s in symbols:
            oldest_date = oldest_by_symbol_id.get(s.id)
            if oldest_date is not None:
                plan.append((s, oldest_date))

        print(f"ACCEPTED symbols with no listed_date: {len(symbols)}")
        print(f"Of those, with a price_snapshots oldest date available: {len(plan)}")
        print(f"Will remain NULL (no price_snapshots rows at all): {len(symbols) - len(plan)}")
        print()
        print("Sample of planned updates (symbol, isin, NULL -> new listed_date):")
        for s, oldest_date in plan[:15]:
            print(f"  {s.symbol:<15} {s.isin:<16} NULL -> {oldest_date}")
        if len(plan) > 15:
            print(f"  ... and {len(plan) - 15} more")

        if not args.apply:
            print()
            print("[PLAN ONLY] No DB changes made. Re-run with --apply to write these.")
            return

        print()
        print(f"Applying {len(plan)} updates to symbols.listed_date ...")
        for s, oldest_date in plan:
            s.listed_date = oldest_date
        db.commit()
        print(f"Done. Updated {len(plan)} rows in symbols.listed_date.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
