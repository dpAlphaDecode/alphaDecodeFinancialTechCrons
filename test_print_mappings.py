"""
Prints every metric mapping used by the financials and stock-details store
paths, for eyeballing/debugging without touching the DB or GDFL API:

  - financials (financial_metrics.py): one label-mapping dict per XBRL
    taxonomy (non_banking, banking, life_insurance, gen_insurance, industry,
    others), used by extract_metrics_with_priority() to fill the
    `financials` table.
  - stock details (fetch_historical_financial_stock_details.py): the
    Detailed_page_keys.xlsx-derived {statement_type: {sector: {parameter:
    expression}}} mapping, used by extract_detailed_metrics() to fill
    pnl_financials/balance_sheet_financials/cashflow_financials.

Usage:
    python test_print_mappings.py
"""
from __future__ import annotations

import json

import financial_metrics as fm
from fetch_historical_financial_stock_details import load_metric_mappings

FINANCIALS_MAPPINGS = {
    "non_banking": fm.matric_mapping_non_banking,
    "banking": fm.matric_mapping_banking,
    "life_insurance": fm.matric_mapping_life_insurance,
    "gen_insurance": fm.matric_mapping_gen_insurance,
    "industry": fm.matric_mapping_industry,
    "others": fm.matric_mapping_others,
}


def print_financials_mappings() -> None:
    print("=" * 80)
    print("FINANCIALS mappings (financial_metrics.py) -- used for the `financials` table")
    print("=" * 80)
    for taxonomy, mapping in FINANCIALS_MAPPINGS.items():
        print(f"\n--- {taxonomy} ({len(mapping)} metric(s)) ---")
        for metric_name, expr in mapping.items():
            print(f"  {metric_name!r}: {expr!r}")


def print_stock_details_mappings() -> None:
    print("\n" + "=" * 80)
    print("STOCK DETAILS mappings (Detailed_page_keys.xlsx) -- used for pnl_financials/")
    print("balance_sheet_financials/cashflow_financials")
    print("=" * 80)
    mappings = load_metric_mappings()
    for stmt_type, by_sector in mappings.items():
        print(f"\n### {stmt_type} ###")
        for sector, sector_mapping in by_sector.items():
            print(f"\n--- {stmt_type} / {sector} ({len(sector_mapping)} metric(s)) ---")
            for metric_name, expr in sector_mapping.items():
                print(f"  {metric_name!r}: {expr!r}")


def main() -> None:
    print_financials_mappings()
    print_stock_details_mappings()

    print("\n" + "=" * 80)
    print("Summary (JSON)")
    print("=" * 80)
    print(json.dumps({"financials": FINANCIALS_MAPPINGS, "stock_details": load_metric_mappings()}, indent=2))


if __name__ == "__main__":
    main()
