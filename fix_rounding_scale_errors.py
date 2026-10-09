"""
Auto-fixes a specific, narrow class of bad data in `financials`: a single
quarterly value that is some clean power of ten (10x, 100x, ..., up to 1e8x)
larger than a neighboring quarter's value for the same symbol+metric_name --
e.g. 1,520,000 stored as 152,000,000,000 (exactly 1e5x). This is the
"rounding/scale" bug pattern, not a real corporate action -- real values
essentially never land on an exact power-of-ten multiple of the adjacent
quarter.

Detection, per (symbol, metric_name) quarterly series: every value is
compared against that symbol's *baseline* -- the most frequently repeated
value across its whole quarterly history (real filings repeat the same
figure quarter after quarter when nothing changed; a scale bug is almost
always a minority of quarters next to a clear majority baseline). A row is
flagged, and corrected by snapping it directly to the baseline (not by a
computed division -- some of these bugs don't land on an exactly clean
power of ten, e.g. a transposed-digit typo mixed with the scale error), if:
  1. it is NOT itself within MATCH_TOLERANCE of the baseline,
  2. value/baseline (or baseline/value) is within RATIO_TOLERANCE of a
     clean 10**n (n=1..8) -- real corporate actions essentially never
     multiply equity share capital by an exact power of ten, so this
     catches both single-quarter blips and multi-quarter runs of the same
     bug, and
  3. the baseline itself is well-established (appears in at least
     BASELINE_MIN_COUNT quarters) -- otherwise there's no reliable anchor
     to correct against.
A real, sustained level change (e.g. an actual bonus share issue) is left
alone because the ratio to baseline for a real change is essentially never
a clean power of ten.

This only touches `financials` (the headline-metric table used for the
qoq_large_changes.xlsx report), and by default only metric_name='Equity
Share Capital' -- pass --metric to widen it.

Usage:
    python fix_rounding_scale_errors.py                  # plan only, no DB writes
    python fix_rounding_scale_errors.py --apply           # actually write the fixes
    python fix_rounding_scale_errors.py --metric "Net Profit" --apply
"""
from __future__ import annotations

import argparse
import math

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

from db.session import SessionLocal, engine
from db.models.financials import Financial

load_dotenv()

RATIO_TOLERANCE = 0.02   # how close value/baseline must be to a clean 10**n
MATCH_TOLERANCE = 0.01   # how close a value must be to baseline to count as "already fine"
BASELINE_MIN_COUNT = 2   # baseline value must repeat at least this many times to be trusted
MAX_POWER = 8
MAX_PLAUSIBLE_BASELINE = 1e11  # a baseline at/above this is itself almost certainly wrong (a
                                 # systemic bug spanning most of the symbol's history, not a
                                 # per-quarter blip) -- these need separate manual review, since
                                 # correcting outliers *toward* an implausible baseline would make
                                 # the data worse, not better

QUERY = """
    SELECT f.id, s.symbol AS symbol, f.metric_name AS metric_name,
           f.date AS date, f.metric_value AS metric_value
    FROM financials f
    JOIN symbols s ON s.id = f.symbol_id
    WHERE f.frequency = 'quarterly' AND f.metric_name = :metric
    ORDER BY s.symbol, f.date
"""


def load_series(metric: str) -> pd.DataFrame:
    df = pd.read_sql(text(QUERY), engine, params={"metric": metric})
    df["metric_value"] = pd.to_numeric(df["metric_value"], errors="coerce")
    return df


def detect_clean_power_of_ten(ratio: float) -> int | None:
    """Returns n if ratio is within RATIO_TOLERANCE of 10**n (n=1..MAX_POWER), else None."""
    if ratio <= 1:
        return None
    n = round(math.log10(ratio))
    if n < 1 or n > MAX_POWER:
        return None
    if abs(ratio / (10 ** n) - 1) <= RATIO_TOLERANCE:
        return n
    return None


def _is_close(a: float, b: float, tol: float) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= tol


def compute_baseline(values: pd.Series) -> tuple[float, int] | None:
    """
    (value, count) to treat as "correct" for this series. Raw mode (most
    frequent value) is unsafe on its own: a scale bug that persisted for
    many consecutive quarters can outnumber the genuinely correct quarters
    (e.g. a bug lasting 13 quarters vs. 6 correct ones either side of it).

    Real series essentially never multiply by a clean power of ten and then
    reverse that exact multiple later -- a company doesn't 100,000x its
    share capital and then divide it back down. So when the first and last
    non-zero values in chronological order are close to each other, that
    boundary-agreed value is trusted as the baseline even if a corrupted
    run in the middle outnumbers it. Falls back to plain mode when the
    boundaries don't agree (no safe anchor either way).
    """
    nonzero = values[(values.notna()) & (values != 0)]
    if nonzero.empty:
        return None
    rounded = nonzero.round(2)
    counts = rounded.value_counts()

    first_val, last_val = float(rounded.iloc[0]), float(rounded.iloc[-1])
    if _is_close(first_val, last_val, MATCH_TOLERANCE):
        anchor = first_val
        anchor_count = int(((rounded - anchor).abs() <= anchor * MATCH_TOLERANCE).sum())
        return anchor, anchor_count

    top = counts.idxmax()
    return float(top), int(counts.loc[top])


def find_fixes(df: pd.DataFrame) -> dict[int, dict]:
    fixes: dict[int, dict] = {}

    for symbol, group in df.groupby("symbol", sort=False):
        group = group.sort_values("date").reset_index(drop=True)
        baseline_info = compute_baseline(group["metric_value"])
        if baseline_info is None:
            continue
        baseline, baseline_count = baseline_info
        if baseline == 0 or baseline_count < BASELINE_MIN_COUNT:
            continue
        if baseline >= MAX_PLAUSIBLE_BASELINE:
            continue  # baseline itself is implausible -- likely a systemic bug, needs manual review

        for i in range(len(group)):
            row = group.iloc[i]
            v = row["metric_value"]
            if pd.isna(v) or v == 0:
                continue
            if _is_close(v, baseline, MATCH_TOLERANCE):
                continue  # already matches baseline, nothing to fix

            if v > baseline:
                n = detect_clean_power_of_ten(v / baseline)
            else:
                n = detect_clean_power_of_ten(baseline / v)
            if n is None:
                continue  # not a clean power-of-ten relationship to baseline -- leave for manual review

            fixes[int(row["id"])] = {
                "symbol": symbol,
                "metric_name": row["metric_name"],
                "date": row["date"],
                "old_value": v,
                "new_value": baseline,
                "baseline_count": baseline_count,
                "factor": 10 ** n,
            }
    return fixes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metric", default="Equity Share Capital", help="metric_name to scan/fix.")
    parser.add_argument("--apply", action="store_true", help="Actually write the fixes. Without this, plan only.")
    args = parser.parse_args()

    df = load_series(args.metric)
    print(f"Loaded {len(df)} quarterly '{args.metric}' rows across {df['symbol'].nunique()} symbols.")

    fixes = find_fixes(df)
    print(f"Detected {len(fixes)} row(s) with a clean power-of-ten scale error.")

    preview = sorted(fixes.items(), key=lambda kv: kv[1]["symbol"])
    for row_id, info in preview[:20]:
        op = "/" if info["new_value"] < info["old_value"] else "x"
        print(
            f"  id={row_id:<8} {info['symbol']:<12} {str(info['date']):<12} "
            f"{info['old_value']:>20,.2f} -> {info['new_value']:>16,.2f}  ({op}{info['factor']:,})"
        )
    if len(fixes) > 20:
        print(f"  ... and {len(fixes) - 20} more")

    if not args.apply:
        print("\n[PLAN ONLY] No DB changes made. Re-run with --apply to write these.")
        return

    db = SessionLocal()
    try:
        applied = 0
        for row_id, info in fixes.items():
            row = db.query(Financial).filter_by(id=row_id).first()
            if row is None:
                continue
            row.metric_value = float(info["new_value"])
            applied += 1
        db.commit()
        print(f"\nApplied {applied} fix(es) to `financials`.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
