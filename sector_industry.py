"""
Shared GetSectoralClassification helpers used by fetch_bhavcopy_symbols.py
(classifies every symbol it just upserted) and fetch_missing_sector_industry.py
(backfills just the `symbols` rows left with a null sector_id/industry_id).

Docs: https://docs.globaldatafeeds.in/getsectoralclassification-15575605e0
Response rows are wrapped in {"Value": [...]} with PascalCase fields; this
module reads `Symbol`, `Sector`, and `Industry` off each row (ScripCode,
CompanyName, ISIN, etc. aren't needed here). GetSectoralClassification caps
`instrumentIdentifiers` at gdfl.client.MAX_INSTRUMENTS_PER_REQUEST (25) per
call, so symbols are batched into chunks of that size.

Every call is made with exchange=EXCHANGE ("BSE") regardless of which
exchange the symbol itself is listed on -- GDFL's sectoral classification
data is sourced from BSE, so this is hardcoded rather than threaded through
from callers.
"""
from __future__ import annotations

import logging

from db.models.industry import Industry
from db.models.sector import Sector
from db.models.symbol import Symbol
from gdfl import GDFLAPIError, GDFLClient
from gdfl.client import MAX_INSTRUMENTS_PER_REQUEST

logger = logging.getLogger(__name__)

CHUNK_SIZE = MAX_INSTRUMENTS_PER_REQUEST
EXCHANGE = "BSE"


def chunked(items: list, size: int = CHUNK_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_classification_by_symbol(client: GDFLClient, symbols: list[str]) -> dict[str, dict[str, str]]:
    """
    Calls GetSectoralClassification (always with exchange=EXCHANGE) in
    batches of <= CHUNK_SIZE symbols. Returns {symbol.upper(): {"sector":
    ..., "industry": ...}} for every row that came back with a non-empty
    Sector or Industry; symbols the API has no classification for (or that
    errored) are simply absent from the dict.
    """
    result: dict[str, dict[str, str]] = {}
    for batch in chunked(symbols):
        try:
            response = client.get_sectoral_classification(exchange=EXCHANGE, instrument_identifiers=batch)
            logger.debug("GetSectoralClassification response: %s", response)
        except GDFLAPIError as exc:
            logger.error("GetSectoralClassification failed for %s: %s", batch, exc)
            continue
        rows = response.get("Value") if isinstance(response, dict) else None
        for row in rows or []:
            symbol = str(row.get("Symbol") or "").strip().upper()
            sector = str(row.get("Sector") or "").strip()
            industry = str(row.get("Industry") or "").strip()
            if not symbol or (not sector and not industry):
                continue
            result[symbol] = {"sector": sector, "industry": industry}
    return result


def get_or_create_sector(db, cache: dict[str, Sector], name: str) -> Sector | None:
    """
    Looks up a Sector by (case-insensitive) name, creating -- and flushing,
    so it has an id -- a new row the first time this name is seen in `cache`.
    """
    name = (name or "").strip()
    if not name:
        return None
    key = name.upper()
    cached = cache.get(key)
    if cached is not None:
        return cached

    existing = db.query(Sector).filter(Sector.name.ilike(name)).one_or_none()
    if existing is None:
        existing = Sector(name=name)
        db.add(existing)
        db.flush()
        logger.info("new sector: %r (id=%s)", name, existing.id)
    cache[key] = existing
    return existing


def get_or_create_industry(
    db, cache: dict[str, Industry], name: str, sector_name: str | None
) -> Industry | None:
    """
    Looks up an Industry by (case-insensitive) name, creating -- with its
    `sector` text column set from `sector_name` -- a new row the first time
    this name is seen in `cache`. Does not overwrite `sector` on an existing
    row (a name conflict there would be a data question for a human, not
    something to silently resolve here).
    """
    name = (name or "").strip()
    if not name:
        return None
    key = name.upper()
    cached = cache.get(key)
    if cached is not None:
        return cached

    existing = db.query(Industry).filter(Industry.name.ilike(name)).one_or_none()
    if existing is None:
        existing = Industry(name=name, sector=sector_name or None)
        db.add(existing)
        db.flush()
        logger.info("new industry: %r (id=%s)", name, existing.id)
    cache[key] = existing
    return existing


def apply_sector_industry(
    db,
    client: GDFLClient,
    rows: list[Symbol],
    dry_run: bool = False,
) -> dict[str, int]:
    """
    For every Symbol row in `rows`, fetches its Sector/Industry via
    GetSectoralClassification (exchange=EXCHANGE, i.e. "BSE", for all of
    them regardless of the row's own exchange) and applies them -- creating
    new sectors/industries rows first when a name hasn't been seen before,
    then pointing the symbol row's sector_id/industry_id at them. Rows with
    a blank `.symbol`, or for which the API returned neither Sector nor
    Industry, are left untouched.

    Caller owns the transaction (commit/rollback) -- this only flushes, so
    a dry run can inspect would-be ids without them becoming visible to
    other sessions.
    """
    stats = {"api_matched": 0, "sector_updated": 0, "industry_updated": 0, "unchanged": 0}
    by_symbol = {row.symbol.strip().upper(): row for row in rows if row.symbol and row.symbol.strip()}
    if not by_symbol:
        return stats

    classifications = fetch_classification_by_symbol(client, list(by_symbol.keys()))
    sector_cache: dict[str, Sector] = {}
    industry_cache: dict[str, Industry] = {}
    prefix = "[DRY RUN] " if dry_run else ""

    for symbol, classification in classifications.items():
        row = by_symbol.get(symbol)
        if row is None:
            continue
        stats["api_matched"] += 1
        sector_name = classification["sector"]
        industry_name = classification["industry"]

        sector = get_or_create_sector(db, sector_cache, sector_name)
        industry = get_or_create_industry(db, industry_cache, industry_name, sector_name or None)

        changed = []
        if sector is not None and row.sector_id != sector.id:
            changed.append(f"sector -> {sector_name!r}")
            row.sector_id = sector.id
            stats["sector_updated"] += 1
        if industry is not None and row.industry_id != industry.id:
            changed.append(f"industry -> {industry_name!r}")
            row.industry_id = industry.id
            stats["industry_updated"] += 1

        if changed:
            logger.info("%s%s: %s", prefix, symbol, ", ".join(changed))
        else:
            stats["unchanged"] += 1

    return stats
