"""
For every (symbol, metric_name) in the `financials` table, looks at only
quarterly rows (frequency='quarterly': Earning Per Share, Outstanding Share,
Face Value Of Equity Share Capital, sales, Net Profit, Equity Share Capital)
ordered by date, and computes the %% change between each pair of
consecutive available quarters for that symbol+metric. "Consecutive" means
consecutive *stored* quarters -- if a quarter is missing in the DB, the
comparison still happens between whatever two quarters are adjacent in the
data, it does not require calendar adjacency.

Flags every pair where abs(%% change) > 300%%, and writes them to an xlsx
report, one row per flagged (symbol, metric_name, quarter pair).

Rows where the earlier quarter's value is 0 are skipped (%% change is
undefined/infinite in that case) and counted separately in the summary.

Read-only -- never writes to the DB.

Usage:
    python find_large_qoq_changes.py
    python find_large_qoq_changes.py --threshold 300
    python find_large_qoq_changes.py --output report.xlsx
"""
from __future__ import annotations

import argparse

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

from db.session import engine

load_dotenv()

DEFAULT_THRESHOLD = 300.0
DEFAULT_OUTPUT = "qoq_large_changes.xlsx"

QUERY = """
    SELECT s.symbol AS symbol, s.name AS company_name, f.metric_name AS metric_name,
           f.year AS year, f.period AS period, f.date AS date,
           f.metric_value AS metric_value
    FROM financials f
    JOIN symbols s ON s.id = f.symbol_id
    WHERE f.frequency = 'quarterly'
    ORDER BY s.symbol, f.metric_name, f.date
"""


def load_quarterly_financials() -> pd.DataFrame:
    df = pd.read_sql(text(QUERY), engine)
    df["metric_value"] = pd.to_numeric(df["metric_value"], errors="coerce")
    return df


def find_large_changes(df: pd.DataFrame, threshold: float) -> tuple[pd.DataFrame, int]:
    df = df.sort_values(["symbol", "metric_name", "date"]).reset_index(drop=True)

    grouped = df.groupby(["symbol", "company_name", "metric_name"], sort=False)
    df["prev_value"] = grouped["metric_value"].shift(1)
    df["prev_year"] = grouped["year"].shift(1)
    df["prev_period"] = grouped["period"].shift(1)
    df["prev_date"] = grouped["date"].shift(1)

    has_pair = df["prev_value"].notna() & df["metric_value"].notna()
    zero_base = has_pair & (df["prev_value"] == 0)
    skipped_zero_base = int(zero_base.sum())

    valid = has_pair & (df["prev_value"] != 0)
    df.loc[valid, "pct_change"] = (
        (df.loc[valid, "metric_value"] - df.loc[valid, "prev_value"]) / df.loc[valid, "prev_value"].abs() * 100
    )

    flagged = df[valid & (df["pct_change"].abs() > threshold)].copy()
    flagged["from_quarter"] = flagged["prev_year"].astype(int).astype(str) + " " + flagged["prev_period"]
    flagged["to_quarter"] = flagged["year"].astype(str) + " " + flagged["period"]

    out = flagged[[
        "symbol", "company_name", "metric_name", "from_quarter", "prev_value",
        "to_quarter", "metric_value", "pct_change",
    ]].rename(columns={"metric_value": "to_value", "prev_value": "from_value"})

    out = out.sort_values("pct_change", key=lambda s: s.abs(), ascending=False).reset_index(drop=True)
    return out, skipped_zero_base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="%% change magnitude to flag (default 300).")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output .xlsx path.")
    args = parser.parse_args()

    df = load_quarterly_financials()
    print(f"Loaded {len(df)} quarterly financials rows across {df['symbol'].nunique()} symbols, {df['metric_name'].nunique()} metric(s).")

    flagged, skipped_zero_base = find_large_changes(df, args.threshold)

    flagged.to_excel(args.output, index=False, sheet_name="Large QoQ Changes")

    print(f"Flagged {len(flagged)} (symbol, metric, quarter-pair) row(s) with |%% change| > {args.threshold}%%.")
    print(f"Skipped {skipped_zero_base} pair(s) where the prior quarter's value was 0 (%% change undefined).")
    print(f"Wrote report to {args.output}")

    if not flagged.empty:
        print("\nBy metric_name:")
        print(flagged["metric_name"].value_counts().to_string())


if __name__ == "__main__":
    main()
