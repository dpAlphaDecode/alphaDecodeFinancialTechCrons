"""
Prints the full "Equity Share Capital" history (all frequencies/periods) for
one symbol from the `financials` table, for manual review against the
flagged QoQ changes in qoq_large_changes.xlsx.

Usage:
    python show_equity_share_capital.py SYMBOL
"""
from __future__ import annotations

import sys

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

from db.session import engine

load_dotenv()

QUERY = """
    SELECT f.id, f.date, f.frequency, f.period, f.year,
           f.report_type, f.metric_value
    FROM financials f
    JOIN symbols s ON s.id = f.symbol_id
    WHERE s.symbol = :symbol AND f.metric_name = 'Equity Share Capital'
    ORDER BY f.date
"""


def show(symbol: str) -> pd.DataFrame:
    df = pd.read_sql(text(QUERY), engine, params={"symbol": symbol})
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 160)
    print(f"=== {symbol} -- Equity Share Capital ({len(df)} row(s)) ===")
    print(df.to_string(index=False, formatters={"metric_value": lambda v: f"{v:,.2f}"}))
    return df


if __name__ == "__main__":
    show(sys.argv[1])
