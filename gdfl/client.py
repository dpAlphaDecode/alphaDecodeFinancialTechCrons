"""
Python client for the GlobalDataFeeds (GDFL) Fundamental / Corporate Data API.

Docs:  https://docs.globaldatafeeds.in/
Endpoint paths and parameter sets were taken from the per-endpoint docs pages
(e.g. https://docs.globaldatafeeds.in/getfinancialresults-15575585e0), then
verified against the live server and corrected where the docs were wrong:
date-range params are `fromDate`/`toDate` on the real API, not `from`/`to`
as documented (GetShpItems is the one exception -- see its docstring).
Response payloads also use PascalCase keys (e.g. `Value`) in practice, not
the lowercase `value`/`count` shown in the docs samples.

Auth model: every request carries an `accessKey` query parameter issued by
GlobalDataFeeds. There is no separate token/OAuth flow documented -- you get
the key from GlobalDataFeeds directly (sales/support), not via a public
self-service signup endpoint.
"""
from __future__ import annotations

import collections
import datetime as _dt
import os
import threading
import time
from typing import Iterable, Sequence

import requests

from gdfl.exceptions import GDFLAPIError

DEFAULT_BASE_URL = "https://nimblerest.lisuns.com:4532/"
MAX_INSTRUMENTS_PER_REQUEST = 25
DEFAULT_RATE_LIMIT_PER_HOUR = 1800

_DateLike = str | _dt.date


class _RateLimiter:
    """Sliding-window limiter: blocks until fewer than `max_calls` requests have gone out in the trailing `period` seconds."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if len(self._calls) >= self.max_calls:
                sleep_for = self.period - (now - self._calls[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                self._prune(time.monotonic())
            self._calls.append(time.monotonic())


def _fmt_date(value: _DateLike) -> str:
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    return value


def _to_epoch_seconds(value: _DateLike) -> int:
    """Convert a date (or 'YYYY-MM-DD' string) to Unix epoch seconds at UTC midnight."""
    if isinstance(value, str):
        value = _dt.datetime.strptime(value, "%Y-%m-%d").date()
    return int(_dt.datetime(value.year, value.month, value.day, tzinfo=_dt.timezone.utc).timestamp())


def _join_instruments(instruments: str | Iterable[str]) -> str:
    if isinstance(instruments, str):
        return instruments
    items: Sequence[str] = list(instruments)
    if len(items) > MAX_INSTRUMENTS_PER_REQUEST:
        raise ValueError(
            f"GDFL APIs accept at most {MAX_INSTRUMENTS_PER_REQUEST} instruments "
            f"per request, got {len(items)}."
        )
    return "+".join(items)


class GDFLClient:
    """Thin wrapper around the GDFL Fundamental/Corporate Data REST API."""

    def __init__(
        self,
        access_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        default_exchange: str = "NSE",
        timeout: float = 30.0,
        session: requests.Session | None = None,
        rate_limit_per_hour: int | None = DEFAULT_RATE_LIMIT_PER_HOUR,
    ):
        self.access_key = access_key or os.environ.get("GDFL_ACCESS_KEY")
        if not self.access_key:
            raise ValueError(
                "An accessKey is required. Pass access_key=... or set the "
                "GDFL_ACCESS_KEY environment variable."
            )
        self.base_url = base_url.rstrip("/")
        self.default_exchange = default_exchange
        self.timeout = timeout
        self.session = session or requests.Session()
        # GDFL enforces a 1800 requests/hour cap on this key; pass
        # rate_limit_per_hour=None to disable (e.g. in tests with a mocked session).
        self._rate_limiter = (
            _RateLimiter(max_calls=rate_limit_per_hour, period=3600.0)
            if rate_limit_per_hour
            else None
        )

    # ------------------------------------------------------------------
    # core request plumbing
    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict | str:
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()

        clean_params = {"accessKey": self.access_key}
        for key, value in params.items():
            if value is None:
                continue
            clean_params[key] = value
        clean_params.setdefault("format", "Json")

        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, params=clean_params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GDFLAPIError(f"Request to {path} failed: {exc}") from exc

        if resp.status_code != 200:
            raise GDFLAPIError(
                f"GDFL API returned HTTP {resp.status_code} for {path}: {resp.text[:500]}",
                status_code=resp.status_code,
                payload=resp.text,
            )

        if clean_params["format"] == "Json":
            try:
                return resp.json()
            except ValueError as exc:
                raise GDFLAPIError(
                    f"Could not parse JSON response from {path}: {resp.text[:500]}"
                ) from exc
        return resp.text

    # ------------------------------------------------------------------
    # 1. Corporate announcements
    # ------------------------------------------------------------------
    def get_corporate_announcements_categories(self, exchange: str | None = None, format: str = "Json"):
        return self._get(
            "/GetCorporateAnnouncementsCategories",
            {"exchange": exchange or self.default_exchange, "format": format},
        )

    def get_corporate_announcements(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        instrument_identifier: str | None = None,
        descriptor: str | None = None,
        descriptor_id: str | None = None,
        category: str | None = None,
        has_meeting: bool | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetCorporateAnnouncements",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "instrumentIdentifier": instrument_identifier,
                "descriptor": descriptor,
                "descriptorId": descriptor_id,
                "category": category,
                "hasMeeting": has_meeting,
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 2. Corporate actions
    # ------------------------------------------------------------------
    def get_corporate_actions_categories(self, exchange: str | None = None, format: str = "Json"):
        return self._get(
            "/GetCorporateActionsCategories",
            {"exchange": exchange or self.default_exchange, "format": format},
        )

    def get_corporate_actions(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        instrument_identifier: str | None = None,
        action_type: str | None = None,
        purpose_code: str | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetCorporateActions",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "instrumentIdentifier": instrument_identifier,
                "actionType": action_type,
                "purposeCode": purpose_code,
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 3. Results calendar / financial results / ratios
    # ------------------------------------------------------------------
    def get_results_calendar(
        self,
        exchange: str | None = None,
        instrument_identifiers: str | Iterable[str] | None = None,
        range: str = "Next7days",
        format: str = "Json",
    ):
        return self._get(
            "/GetResultsCalendar",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "range": range,
                "format": format,
            },
        )

    def get_financial_results_items(
        self,
        year: int,
        period: str,
        exchange: str | None = None,
        instrument_identifiers: str | Iterable[str] | None = None,
        nature_of_reports: str | None = None,
        types: str | None = None,
        format: str = "Json",
    ):
        """period: one of QM, M12, QJ, QS, M6, QD, M9"""
        return self._get(
            "/GetFinancialResultsItems",
            {
                "exchange": exchange or self.default_exchange,
                "Year": year,
                "periods": period,
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "natureOfReports": nature_of_reports,
                "types": types,
                "format": format,
            },
        )

    def get_financial_results(
        self,
        exchange: str = "BSE",
        instrument_identifier: str = "RELIANCE",
        year: int = 2024,
        nature_of_report: str = "Consolidated",
        type: str = "ProfitLoss",
        period: str = "QD",
        format: str = "Json",
    ):
        """
        GET /GetFinancialResults

        exchange: e.g. BSE (default "BSE")
        instrument_identifier: ticker symbol, e.g. RELIANCE (default "RELIANCE")
        year: financial year, e.g. 2024 for FY-2024-2025 (default 2024)
        nature_of_report: Standalone | Consolidated (default "Consolidated")
        type: ProfitLoss | BalanceSheet | CashFlow | RelatedPartyTransactions | FR (default "ProfitLoss")
        period: QM | M12 | QJ | QS | M6 | QD | M9 (default "QD")
        format: Json | Xml | Csv | CsvContent (default "Json")
        """
        return self._get(
            "/GetFinancialResults",
            {
                "exchange": exchange,
                "instrumentIdentifier": instrument_identifier,
                "year": year,
                "natureOfReport": nature_of_report,
                "type": type,
                "period": period,
                "format": format,
            },
        )

    def get_financial_ratios(
        self,
        instrument_identifier: str,
        type: str,
        nature_of_report: str,
        exchange: str | None = None,
        date: _DateLike | None = None,
        period: str | None = None,
        year: int | None = None,
        format: str = "Json",
    ):
        """
        type: Realtime | EOD | Quarter
        period (when type=Quarter): QM | QJ | QS | QD | Latest
        """
        return self._get(
            "/GetFinancialRatios",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifier": instrument_identifier,
                "type": type,
                "natureOfReport": nature_of_report,
                "date": _fmt_date(date) if date else None,
                "period": period,
                "year": year,
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 4. Sectoral classification
    # ------------------------------------------------------------------
    def get_sectoral_classification(
        self,
        exchange: str | None = None,
        instrument_identifiers: str | Iterable[str] | None = None,
        sector: str | None = None,
        industry: str | None = None,
        basic_industry: str | None = None,
        from_date: _DateLike | None = None,
        to_date: _DateLike | None = None,
        format: str = "Json",
    ):
        """At least one filter (instrument_identifiers/sector/industry/basic_industry/dates) is required by the API."""
        return self._get(
            "/GetSectoralClassification",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "sector": sector,
                "industry": industry,
                "basicIndustry": basic_industry,
                "fromDate": _fmt_date(from_date) if from_date else None,
                "toDate": _fmt_date(to_date) if to_date else None,
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 5. Shareholding pattern
    # ------------------------------------------------------------------
    def get_shp_items(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        instrument_identifiers: str | Iterable[str] | None = None,
        period: str | None = None,
        format: str = "Json",
    ):
        """
        Unlike every other date-range endpoint in this client, GetShpItems'
        `from`/`to` fields are typed as Int32 on the live server, not dates:
        date strings ("yyyy-MM-dd", ISO, "dd-MMM-yyyy", epoch milliseconds)
        are all rejected as "value is not valid", while epoch *seconds*,
        yyyyMMdd, ddMMyyyy, and Excel serial-date integers all pass model
        binding (confirmed by reaching the account's "Function not enabled"
        gate instead of a validation error). This client sends epoch
        seconds (UTC midnight) since that's the standard numeric date form;
        if your account has this function enabled and it turns out to want
        yyyyMMdd/ddMMyyyy/serial instead, adjust `_to_epoch_seconds` calls
        below accordingly.
        """
        return self._get(
            "/GetShpItems",
            {
                "exchange": exchange or self.default_exchange,
                "from": _to_epoch_seconds(from_date),
                "to": _to_epoch_seconds(to_date),
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "period": period,
                "format": format,
            },
        )

    def get_shp(
        self,
        instrument_identifier: str,
        exchange: str | None = None,
        period: str | None = None,
        date: _DateLike | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetSHP",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifier": instrument_identifier,
                "period": period,
                "date": _fmt_date(date) if date else None,
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 6. Market cap
    # ------------------------------------------------------------------
    def get_scrip_mcap(
        self,
        exchange: str | None = None,
        instrument_identifier: str | None = None,
        from_date: _DateLike | None = None,
        to_date: _DateLike | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetScripMCap",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifier": instrument_identifier,
                "fromDate": _fmt_date(from_date) if from_date else None,
                "toDate": _fmt_date(to_date) if to_date else None,
                "format": format,
            },
        )

    def get_exchange_mcap(self, date: _DateLike, exchange: str | None = None, format: str = "Json"):
        return self._get(
            "/GetExchangeMCap",
            {"exchange": exchange or self.default_exchange, "date": _fmt_date(date), "format": format},
        )

    # ------------------------------------------------------------------
    # 7. Company / annual report info
    # ------------------------------------------------------------------
    def get_annual_reports(
        self,
        instrument_identifiers: str | Iterable[str],
        exchange: str | None = None,
        year: int | None = None,
        format: str = "Json",
    ):
        """
        Spec page for this endpoint could not be retrieved (404 at crawl time);
        parameters mirror GetCompanyData's instrument-list pattern. Verify
        against https://docs.globaldatafeeds.in/getannualreports-15575602e0
        before relying on this in production.
        """
        return self._get(
            "/GetAnnualReports",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifiers": _join_instruments(instrument_identifiers),
                "year": year,
                "format": format,
            },
        )

    def get_company_data(
        self,
        instrument_identifiers: str | Iterable[str],
        exchange: str | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetCompanyData",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifiers": _join_instruments(instrument_identifiers),
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 8. Deals / delivery volumes
    # ------------------------------------------------------------------
    def get_bulk_deals(
        self,
        exchange: str | None = None,
        instrument_identifier: str | None = None,
        range: str = "Previous7days",
        format: str = "Json",
    ):
        return self._get(
            "/GetBulkDeals",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifier": instrument_identifier,
                "range": range,
                "format": format,
            },
        )

    def get_block_deals(
        self,
        exchange: str | None = None,
        instrument_identifier: str | None = None,
        range: str = "Previous7days",
        format: str = "Json",
    ):
        return self._get(
            "/GetBlockDeals",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifier": instrument_identifier,
                "range": range,
                "format": format,
            },
        )

    def get_delivery_volumes(
        self,
        exchange: str | None = None,
        instrument_identifier: str | None = None,
        range: str = "Previous7days",
        format: str = "Json",
    ):
        return self._get(
            "/GetDeliveryVolumes",
            {
                "exchange": exchange or self.default_exchange,
                "instrumentIdentifier": instrument_identifier,
                "range": range,
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 9. Bhavcopy / F&O
    # ------------------------------------------------------------------
    def get_bhavcopy_cm(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        instrument_identifiers: str | Iterable[str] | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetBhavCopyCM",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "format": format,
            },
        )

    def get_bhavcopy_fo(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str = "NFO",
        instrument_identifiers: str | Iterable[str] | None = None,
        format: str = "Json",
    ):
        """exchange is typically NFO/BFO for the F&O segment."""
        return self._get(
            "/GetBhavCopyFO",
            {
                "exchange": exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "format": format,
            },
        )

    def get_futures_and_options(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str = "NFO",
        instrument_identifiers: str | Iterable[str] | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetFuturesAndOptions",
            {
                "exchange": exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # Historical OHLCV (GetHistory)
    # ------------------------------------------------------------------
    def get_history(
        self,
        instrument_identifier: str,
        exchange: str | None = None,
        periodicity: str = "DAY",
        period: int = 1,
        from_date: _DateLike | None = None,
        to_date: _DateLike | None = None,
        max_records: int = 0,
        user_tag: str | None = None,
        is_short_identifier: bool = False,
        adjust_splits: bool = True,
        format: str = "Json",
    ):
        """
        Per-instrument OHLCV bars for a date range (defaults to daily bars).

        Documented at https://globaldatafeeds.in/global-datafeeds-apis/
        global-datafeeds-apis/rest-api-documentation/function-gethistory/ --
        unlike every date-range endpoint above (fromDate/toDate strings),
        GetHistory takes `from`/`to` as UNIX epoch seconds, like GetShpItems
        (see get_shp_items()'s docstring) -- passing fromDate/toDate instead
        is silently ignored by the live server rather than rejected, so this
        must use `_to_epoch_seconds()`. periodicity is one of
        TICK/MINUTE/HOUR/DAY/WEEK/MONTH; leaving from/to unset returns
        whatever `max` bars are available (max=0 means all available data).

        Response fields per the docs are LastTradeTime/QuotationLot/
        TradedQty/OpenInterest/Open/High/Low/Close; live responses on this
        account use ALL-CAPS keys instead. Confirmed live for
        exchange="BSE", periodicity="DAY": {"USERTAG": ..., "OHLC": [
        {"OPEN":.., "HIGH":.., "LOW":.., "CLOSE":.., "TRADEDQTY":..,
        "LASTTRADETIME": <epoch ms>, "OPENINTEREST":.., "QUOTATIONLOT":..},
        ...]} -- one dict per bar, `from`/`to` correctly bound the range.
        Other periodicities (e.g. leaving periodicity/from/to unset) instead
        wrap ticks/quotes under a "TICK" key with different per-item fields
        (BUYPRICE/SELLPRICE/LASTTRADEPRICE/... -- no OPEN/HIGH/LOW/CLOSE).

        exchange="NSE" returned "Data for requested exchange is disabled"
        on this account as of 2026-09-24 (BSE worked) -- this account may
        need NSE enabled by GDFL support before this works for NSE symbols.
        """
        params = {
            "exchange": exchange or self.default_exchange,
            "instrumentIdentifier": instrument_identifier,
            "periodicity": periodicity,
            "period": period,
            "max": max_records,
            "userTag": user_tag,
            "isShortIdentifier": "true" if is_short_identifier else "false",
            "AdjustSplits": "true" if adjust_splits else "false",
            "format": format,
        }
        if from_date is not None:
            params["from"] = _to_epoch_seconds(from_date)
        if to_date is not None:
            params["to"] = _to_epoch_seconds(to_date)
        return self._get("/GetHistory", params)

    # ------------------------------------------------------------------
    # 10. Indices / market breadth / turnover
    # ------------------------------------------------------------------
    def get_index_detail(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str = "NSE_IDX",
        instrument_identifiers: str | Iterable[str] | None = None,
        format: str = "Json",
    ):
        """exchange is typically NSE_IDX/BSE_IDX for index data."""
        return self._get(
            "/GetIndexDetail",
            {
                "exchange": exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "format": format,
            },
        )

    def get_circuit_filter_details(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        instrument_identifiers: str | Iterable[str] | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetCircuitFilterDetails",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "instrumentIdentifiers": _join_instruments(instrument_identifiers) if instrument_identifiers else None,
                "format": format,
            },
        )

    def get_statistics_group_ab_companies(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        groups: str | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetStatisticsGroupAbCompanies",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "groups": groups,
                "format": format,
            },
        )

    def get_index_highlights(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetIndexHighlights",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "format": format,
            },
        )

    def get_total_trade_highlights(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetTotalTradeHighlights",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "format": format,
            },
        )

    def get_scrip_group_trade_highlights(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        format: str = "Json",
    ):
        return self._get(
            "/GetScripGroupTradeHighlights",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "format": format,
            },
        )

    def get_turnover_details_of_top15_scrips_of_a_group(
        self,
        from_date: _DateLike,
        to_date: _DateLike,
        exchange: str | None = None,
        format: str = "Json",
    ):
        # NB: the live path uses "...ofAgroup" casing, not "OfAGroup".
        return self._get(
            "/GetTurnoverDetailsOfTop15ScripsofAgroup",
            {
                "exchange": exchange or self.default_exchange,
                "fromDate": _fmt_date(from_date),
                "toDate": _fmt_date(to_date),
                "format": format,
            },
        )

    # ------------------------------------------------------------------
    # 11. Instrument master
    # ------------------------------------------------------------------
    def get_instruments(
        self,
        exchange: str | None = None,
        instrument_type: str | None = None,
        product: str | None = None,
        expiry: str | None = None,
        option_type: str | None = None,
        strike_price: str | None = None,
        series: str | None = None,
        show_dummy_isin: bool | None = None,
        show_etf: bool | None = None,
        show_inter_operable: bool | None = None,
        only_active: bool | None = None,
        detailed_info: bool | None = None,
        format: str = "Json",
    ):
        """
        GET /GetInstruments -- GDFL's instrument-master API. This is a
        different product/response shape from the "Value"-wrapped
        Fundamental/Corporate Data endpoints above: the response here is
        {"INSTRUMENTS": [...]}, with UPPERCASE per-instrument fields
        (IDENTIFIER, DESCRIPTION, ISIN, SERIES, EXCHANGE, ...). Verified
        live against nimblerest.lisuns.com:4532 -- same host as the rest of
        this client, despite being documented on a separate docs page.

        `series` filters by Equity/CASH series (e.g. "A", "B", "T", "Z",
        "ZP", "X", "XT", "P" on BSE); Futures/Options-only params
        (instrument_type/product/expiry/option_type/strike_price) are
        ignored for equities.

        `detailed_info=True` additionally returns TokenNumber,
        LowPriceRange, HighPriceRange, ISIN, Series, 52WeekHigh, 52WeekLow
        (note: those last two come back as string keys "52WeekHigh" /
        "52WeekLow" in the JSON since they start with a digit).
        """
        params = {
            "exchange": exchange or self.default_exchange,
            "instrumentType": instrument_type,
            "product": product,
            "expiry": expiry,
            "optionType": option_type,
            "strikePrice": strike_price,
            "series": series,
            "format": format,
        }
        for key, value in (
            ("showDummyISIN", show_dummy_isin),
            ("showETF", show_etf),
            ("showInterOperable", show_inter_operable),
            ("onlyActive", only_active),
            ("detailedInfo", detailed_info),
        ):
            if value is not None:
                params[key] = "true" if value else "false"
        return self._get("/GetInstruments", params)
