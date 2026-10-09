"""
Fetch listing dates from BSE for all ACCEPTED symbols in the `symbols` table.

No CSV is used -- the symbol/isin list comes straight from the DB. BSE's
ComHeader API needs a numeric BSE scrip code (which isn't stored in the
`symbols` table), so for each symbol we first resolve the scrip code via
BSE's search endpoint (using isin, falling back to symbol), then call
ComHeader with that code.

Run this on a machine whose outbound IP isn't blocked by BSE's Akamai edge
(the dev sandbox this was written in gets a 403 on api.bseindia.com, so none
of this has been verified against live data yet -- run --dry-run first, then
--test, and send back the printed raw JSON so field names can be confirmed).

Usage:
    python fetch_listing_dates_from_bse.py --dry-run       # no network calls, just show what would be fetched
    python fetch_listing_dates_from_bse.py --test           # fetch 5 rows, print raw JSON for both API calls
    python fetch_listing_dates_from_bse.py                  # fetch ALL ACCEPTED symbols -> JSON file
    python fetch_listing_dates_from_bse.py --limit 50        # fetch first 50 ACCEPTED symbols
    python fetch_listing_dates_from_bse.py --output out.json

Output is a JSON array, one object per symbol:
    {symbol, isin, resolved_scrip_code, listing_date_guess, http_status, raw_search_json, raw_comheader_json}
This script never writes to the DB -- it only produces a JSON file for review.
"""

import argparse
import json
import sys
import time

import requests

from db.session import SessionLocal
from db.models.symbol import Symbol

SEARCH_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/ddl_quotesearch/w"
COMHEADER_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/ComHeader/w"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

# Candidate key names for the resolved scrip code in the search response.
# Not yet verified against live data -- check raw_json in --test output.
SCRIP_CODE_KEY_CANDIDATES = ["scrip_cd", "Scrip_cd", "SCRIP_CD", "scripcode", "ScripCode"]

# Candidate key names that might hold the listing date in BSE's ComHeader response.
LISTING_DATE_KEY_CANDIDATES = [
    "DtOfLst", "Dt_of_lst", "DATE_OF_LST", "DateOfListing", "ListingDate",
    "Listing_Date", "LISTING_DATE", "DtOfListing", "Dt_Listing",
]


def load_accepted_symbols():
    db = SessionLocal()
    try:
        accepted = db.query(Symbol).filter(Symbol.final_status == "ACCEPTED").all()
        return [
            {"symbol": s.symbol, "isin": s.isin, "existing_listed_date": s.listed_date}
            for s in accepted
        ]
    finally:
        db.close()


def _first_dict(payload):
    if isinstance(payload, list):
        return payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def extract_by_candidates(payload, candidates):
    d = _first_dict(payload)
    if not d:
        return None
    for key in candidates:
        if key in d and d[key]:
            return d[key]
    return None


def resolve_scrip_code(symbol: str, isin: str, session: requests.Session):
    """Try resolving the BSE numeric scrip code via the search endpoint,
    first by ISIN, then by symbol. Returns (code, raw_payload, status)."""
    for query in filter(None, [isin, symbol]):
        try:
            resp = session.get(
                SEARCH_API_URL,
                params={"Type": "EQ", "text": query},
                headers=HEADERS,
                timeout=15,
            )
            print("===>>", resp)
        except requests.RequestException as exc:
            print(f"    [search] request error: {exc}")
            return None, None, f"ERROR:{exc}"

        print(f"    [search] GET {resp.url} -> {resp.status_code}")
        print(f"    [search] body: {resp.text}")

        if resp.status_code != 200:
            continue
        try:
            payload = resp.json()
        except ValueError:
            continue

        code = extract_by_candidates(payload, SCRIP_CODE_KEY_CANDIDATES)
        if code:
            return code, payload, 200
    return None, None, "not_found"


def fetch_listing_date(scrip_code: str, session: requests.Session, retries: int = 3):
    params = {"quotetype": "EQ", "scripcode": scrip_code, "seriesid": ""}
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(COMHEADER_API_URL, params=params, headers=HEADERS, timeout=15)
        except requests.RequestException as exc:
            print(f"    [comheader] request error: {exc}")
            if attempt == retries:
                return None, f"ERROR:{exc}"
            time.sleep(2 * attempt)
            continue

        print(f"    [comheader] GET {resp.url} -> {resp.status_code}")
        print(f"    [comheader] body: {resp.text}")

        if resp.status_code == 200:
            try:
                return resp.json(), 200
            except ValueError:
                return None, "ERROR:non-json-200"
        if resp.status_code in (403, 429):
            time.sleep(3 * attempt)
            continue
        return None, resp.status_code
    return None, "ERROR:exhausted-retries"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="bse_listing_dates.json")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N ACCEPTED symbols")
    parser.add_argument("--test", action="store_true", help="Shortcut for --limit 5 plus printing raw JSON")
    parser.add_argument("--dry-run", action="store_true", help="Build the list only, no network calls")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to sleep between requests")
    args = parser.parse_args()

    if args.test:
        args.limit = args.limit or 5

    symbols = load_accepted_symbols()
    print(f"ACCEPTED symbols in DB: {len(symbols)}")

    if args.limit:
        symbols = symbols[: args.limit]

    if args.dry_run:
        print(f"[DRY RUN] Would fetch listing dates for {len(symbols)} symbols. No network calls made.")
        dry_rows = [
            {
                "symbol": row["symbol"], "isin": row["isin"],
                "existing_listed_date": row["existing_listed_date"],
                "resolved_scrip_code": None, "listing_date_guess": None,
                "http_status": "dry_run", "raw_search_json": None, "raw_comheader_json": None,
            }
            for row in symbols
        ]
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(dry_rows, f, indent=2, ensure_ascii=False, default=str)
        print(f"Wrote {len(dry_rows)} entries to {args.output} (dry run, no API data).")
        return

    print(f"Fetching listing dates for {len(symbols)} symbols...")

    session = requests.Session()
    try:
        session.get("https://www.bseindia.com/", headers=HEADERS, timeout=15)
    except requests.RequestException:
        pass

    rows_out = []
    for i, row in enumerate(symbols, 1):
        symbol, isin = row["symbol"], row["isin"]

        scrip_code, search_payload, search_status = resolve_scrip_code(symbol, isin, session)

        if not scrip_code:
            rows_out.append({
                "symbol": symbol, "isin": isin, "resolved_scrip_code": None,
                "listing_date_guess": None, "http_status": f"resolve:{search_status}",
                "raw_search_json": search_payload,
                "raw_comheader_json": None,
            })
            print(f"[{i}/{len(symbols)}] {symbol} -> could not resolve scrip code ({search_status})")
            print(json.dumps(search_payload, indent=2) if search_payload else "<no payload>")
            time.sleep(args.sleep)
            continue

        payload, status = fetch_listing_date(scrip_code, session)
        guess = extract_by_candidates(payload, LISTING_DATE_KEY_CANDIDATES) if payload else None

        rows_out.append({
            "symbol": symbol, "isin": isin, "resolved_scrip_code": scrip_code,
            "listing_date_guess": guess, "http_status": status,
            "raw_search_json": search_payload,
            "raw_comheader_json": payload,
        })

        print(f"[{i}/{len(symbols)}] {symbol} (scrip={scrip_code}) -> status={status} guess={guess}")
        print(json.dumps(search_payload, indent=2) if search_payload else "<no search payload>")
        print(json.dumps(payload, indent=2) if payload is not None else "<no comheader payload>")

        time.sleep(args.sleep)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(rows_out, f, indent=2, ensure_ascii=False, default=str)

    resolved_count = sum(1 for r in rows_out if r["resolved_scrip_code"])
    dated_count = sum(1 for r in rows_out if r["listing_date_guess"])
    print(f"\nWrote {len(rows_out)} entries to {args.output}")
    print(f"  resolved scrip code: {resolved_count}/{len(rows_out)}")
    print(f"  got a listing_date_guess: {dated_count}/{len(rows_out)}")
    print("Review listing_date_guess / raw_*_json before applying anything to the DB.")


if __name__ == "__main__":
    sys.exit(main())
