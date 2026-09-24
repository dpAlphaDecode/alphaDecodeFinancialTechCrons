"""
Python client for GlobalDataFeeds' (GDFL) WebSocket "Delayed" market data feed.

Docs:
  https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/how-to-connect-using-websockets-api/
  https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/websockets-api-documentation/authenticate/
  https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/subscribesnapshot-delayed/
  https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/getsnapshot-delayed/
  https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/getexchangesnapshot-delayed/
  https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/delayed-api/gethistory-delayed/

This is a different GDFL product from the `gdfl` (REST Fundamental Data)
package in this repo -- it's a persistent WebSocket connection streaming
market data (price/volume/OI), not corporate/financial data.

Delay model: the "Delayed" endpoints use the exact same MessageType names
and request/response shapes as the realtime WebSocket API. The delay (e.g.
15 minutes) is configured by GDFL against your account/API key on their
backend -- it is not a request parameter here. Confirm the delay in effect
by comparing a message's LastTradeTime (see epoch_to_datetime()) to the
current time.

Connection: host/port are issued per-account by GDFL support, the same way
the accessKey is -- there is no public default to fall back to. Set
GDFL_WS_HOST / GDFL_WS_PORT (or pass host=/port=) once you have them.

Example:
    from gdfl_ws import GDFLDelayedWebSocketClient

    def handle(msg):
        print(msg)

    client = GDFLDelayedWebSocketClient(on_data=handle)
    client.connect()
    client.subscribe_snapshot(exchange="NSE", instrument_identifier="RELIANCE")
    ...
    client.close()
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable

import websocket  # pip install websocket-client

from gdfl_ws.exceptions import GDFLWebSocketError

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_INSTRUMENTS = 25


def epoch_to_datetime(value) -> datetime:
    """
    Convert a GDFL epoch timestamp (LastTradeTime/ServerTime) to a UTC
    datetime. GDFL's own docs disagree on whether these are epoch seconds
    or milliseconds across different pages, so detect by magnitude instead
    of hardcoding a unit.
    """
    value = float(value)
    if value > 1e12:
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


class GDFLDelayedWebSocketClient:
    """Streaming client for GDFL's WebSocket Delayed market data API."""

    def __init__(
        self,
        access_key: str | None = None,
        host: str | None = None,
        port: int | str | None = None,
        use_ssl: bool = False,
        on_data: Callable[[dict], None] | None = None,
        on_error: Callable[[object], None] | None = None,
    ):
        self.access_key = access_key or os.environ.get("GDFL_ACCESS_KEY")
        if not self.access_key:
            raise ValueError(
                "An accessKey is required. Pass access_key=... or set the "
                "GDFL_ACCESS_KEY environment variable."
            )
        self.host = host or os.environ.get("GDFL_WS_HOST")
        self.port = port or os.environ.get("GDFL_WS_PORT")
        if not self.host or not self.port:
            raise ValueError(
                "WebSocket host/port are required. Pass host=/port=... or set "
                "GDFL_WS_HOST / GDFL_WS_PORT env vars (obtain these from GDFL support -- "
                "they are not the same host/port as the REST Fundamental Data API)."
            )
        self.use_ssl = use_ssl
        self.on_data = on_data
        self.on_error = on_error

        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._authenticated = threading.Event()
        self._auth_failed_message: str | None = None

    @property
    def _url(self) -> str:
        scheme = "wss" if self.use_ssl else "ws"
        return f"{scheme}://{self.host}:{self.port}/"

    # ------------------------------------------------------------------
    # connection lifecycle
    # ------------------------------------------------------------------
    def connect(self, timeout: float = 15.0):
        """Open the socket, authenticate, and block until ready (raises on failure/timeout)."""
        self._ws = websocket.WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_ws_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

        if not self._authenticated.wait(timeout):
            raise GDFLWebSocketError(f"Timed out connecting/authenticating to {self._url}")
        if self._auth_failed_message:
            raise GDFLWebSocketError(f"Authentication failed: {self._auth_failed_message}")

    def close(self):
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # requests (MessageType names/payloads match the realtime WS API;
    # data freshness is what differs on the Delayed feed)
    # ------------------------------------------------------------------
    def subscribe_snapshot(self, exchange: str, instrument_identifier: str, periodicity: str = "MINUTE", period: int = 1):
        """Start a continuous delayed snapshot stream for one instrument."""
        self._send({
            "MessageType": "SubscribeSnapshot",
            "Exchange": exchange,
            "InstrumentIdentifier": instrument_identifier,
            "Periodicity": periodicity,
            "Period": period,
            "Unsubscribe": "false",
        })

    def unsubscribe_snapshot(self, exchange: str, instrument_identifier: str, periodicity: str = "MINUTE", period: int = 1):
        self._send({
            "MessageType": "SubscribeSnapshot",
            "Exchange": exchange,
            "InstrumentIdentifier": instrument_identifier,
            "Periodicity": periodicity,
            "Period": period,
            "Unsubscribe": "true",
        })

    def get_snapshot(
        self,
        exchange: str,
        instrument_identifiers: Iterable[str],
        periodicity: str = "MINUTE",
        period: int = 1,
        short_identifiers: bool = False,
    ):
        """One-shot delayed snapshot for up to 25 instruments."""
        identifiers = list(instrument_identifiers)
        if len(identifiers) > MAX_SNAPSHOT_INSTRUMENTS:
            raise ValueError(f"GetSnapshot accepts at most {MAX_SNAPSHOT_INSTRUMENTS} instrument identifiers per request.")
        self._send({
            "MessageType": "GetSnapshot",
            "Exchange": exchange,
            "Periodicity": periodicity,
            "Period": period,
            "isShortIdentifiers": "true" if short_identifiers else "false",
            "InstrumentIdentifiers": [{"Value": i} for i in identifiers],
        })

    def get_exchange_snapshot(self, exchange: str, periodicity: str = "Minute", period: int = 1):
        """One-shot delayed snapshot for every instrument on an exchange."""
        self._send({
            "MessageType": "GetExchangeSnapshot",
            "Exchange": exchange,
            "Periodicity": periodicity,
            "Period": period,
        })

    def get_history(
        self,
        exchange: str,
        instrument_identifier: str,
        periodicity: str = "MINUTE",
        period: int = 5,
        max_records: int = 10,
        short_identifier: bool = False,
        user_tag: str | None = None,
    ):
        payload = {
            "MessageType": "GetHistory",
            "Exchange": exchange,
            "InstrumentIdentifier": instrument_identifier,
            "Periodicity": periodicity,
            "Period": period,
            "Max": max_records,
            "isShortIdentifier": "true" if short_identifier else "false",
        }
        if user_tag:
            payload["UserTag"] = user_tag
        self._send(payload)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _send(self, payload: dict):
        if not self._ws or not self._authenticated.is_set():
            raise GDFLWebSocketError("Not connected/authenticated -- call connect() first.")
        self._ws.send(json.dumps(payload))

    def _on_open(self, ws):
        ws.send(json.dumps({"MessageType": "Authenticate", "Password": self.access_key}))

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)
        except ValueError:
            logger.warning("Non-JSON message from GDFL WS (diagnostic?): %s", message)
            if self.on_error:
                self.on_error(message)
            return

        if data.get("MessageType") == "AuthenticateResult":
            if data.get("Complete"):
                self._authenticated.set()
            else:
                self._auth_failed_message = data.get("Message", "unknown error")
                self._authenticated.set()
            return

        if self.on_data:
            self.on_data(data)
        else:
            logger.info("GDFL WS message: %s", data)

    def _on_ws_error(self, ws, error):
        logger.error("GDFL WS error: %s", error)
        if self.on_error:
            self.on_error(error)

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info("GDFL WS closed: %s %s", close_status_code, close_msg)
