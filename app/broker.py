from __future__ import annotations
import logging
from dataclasses import dataclass
from threading import RLock
from typing import Any
import hashlib
import time


@dataclass
class BrokerConfig:
    exchange_id: str
    api_key: str
    api_secret: str
    password: str = ""
    sandbox: bool = True
    market_type: str = "swap"
    timeout_ms: int = 10_000


class Broker:
    _instances: dict[str, "Broker"] = {}
    _global_lock = RLock()

    def __init__(self, config: BrokerConfig):
        self.config = config
        self.client = None
        # Keep slow/hung order submissions from blocking risk-data reads or the
        # emergency cancellation path. CCXT's request timeout remains the hard bound.
        self._connect_lock = RLock()
        self._order_lock = RLock()
        self._read_lock = RLock()
        self._cancel_lock = RLock()
        self._connect_failure: Exception | None = None
        self._connect_failure_until = 0.0
        self._connect_failure_cooldown_seconds = 5.0
        # Market-data outages must stop producing request storms. This breaker
        # only wraps quote reads; order/cancel/reconciliation calls remain usable
        # so the emergency-stop path is never blocked by a quote-circuit state.
        self._market_data_failure_count = 0
        self._market_data_failure_threshold = 3
        self._market_data_cooldown_seconds = 15.0
        self._market_data_open_until = 0.0
        self._market_data_alert_pending = False
        # Per-call cancellation errors are consumed by emergency-stop reporting.
        self.last_cancel_errors: list[dict[str, str]] = []

    @classmethod
    def get(cls, config: BrokerConfig) -> "Broker":
        credential_id = hashlib.sha256(config.api_key.encode("utf-8")).hexdigest()
        key = f"{config.exchange_id}:{credential_id}:{config.sandbox}:{config.market_type}"
        with cls._global_lock:
            if key not in cls._instances:
                cls._instances[key] = Broker(config)
            return cls._instances[key]

    def connect(self):
        with self._connect_lock:
            if self.client is not None:
                return self.client
            now = time.monotonic()
            if self._connect_failure is not None and now < self._connect_failure_until:
                failure = self._connect_failure
                raise type(failure)(*failure.args) from failure
            if not self.config.api_key or not self.config.api_secret:
                raise RuntimeError("Broker credentials are not configured")
            import ccxt
            if self.config.exchange_id not in ccxt.exchanges:
                raise RuntimeError(f"Unsupported CCXT exchange: {self.config.exchange_id}")
            cls = getattr(ccxt, self.config.exchange_id)
            params = {
                "apiKey": self.config.api_key,
                "secret": self.config.api_secret,
                "enableRateLimit": True,
                "timeout": self.config.timeout_ms,
                "options": {"defaultType": self.config.market_type},
            }
            if self.config.password:
                params["password"] = self.config.password
            client = cls(params)
            # CCXT requires sandbox mode to be enabled before any other exchange call.
            if self.config.sandbox:
                client.set_sandbox_mode(True)
            try:
                client.load_markets()
            except Exception as exc:
                # Do not hammer a down exchange by downloading market metadata on
                # every incoming ticker/risk request. Cool down and fail closed.
                self._connect_failure = exc
                self._connect_failure_until = time.monotonic() + self._connect_failure_cooldown_seconds
                raise
            self._connect_failure = None
            self._connect_failure_until = 0.0
            self.client = client
            return client

    def _c(self):
        return self.client or self.connect()

    def ticker(self, symbol: str) -> dict[str, Any]:
        with self._read_lock:
            now = time.monotonic()
            if now < self._market_data_open_until:
                remaining = max(0.0, self._market_data_open_until - now)
                raise RuntimeError(
                    f"{self.config.exchange_id} market-data circuit is open for {remaining:.1f}s after repeated quote failures"
                )
            try:
                quote = self._c().fetch_ticker(symbol)
                if not isinstance(quote, dict) or not quote:
                    raise RuntimeError("exchange returned an empty or malformed quote payload")
            except Exception as exc:
                self._market_data_failure_count += 1
                if self._market_data_failure_count >= self._market_data_failure_threshold:
                    self._market_data_open_until = time.monotonic() + self._market_data_cooldown_seconds
                    self._market_data_failure_count = 0
                    self._market_data_alert_pending = True
                    logging.getLogger(__name__).warning(
                        "market_data_circuit_open exchange=%s cooldown_seconds=%.1f error_type=%s",
                        self.config.exchange_id, self._market_data_cooldown_seconds, type(exc).__name__,
                    )
                raise
            self._market_data_failure_count = 0
            self._market_data_open_until = 0.0
            return quote

    def consume_market_data_circuit_alert(self) -> bool:
        """Consume the one-shot signal that the quote circuit has newly opened."""
        with self._read_lock:
            pending = bool(self._market_data_alert_pending)
            self._market_data_alert_pending = False
            return pending

    def market_info(self, symbol: str) -> dict[str, Any]:
        with self._read_lock:
            market = self._c().market(symbol)
            return market

    def normalize_amount(self, symbol: str, amount: float) -> float:
        with self._read_lock:
            c = self._c()
            return float(c.amount_to_precision(symbol, amount))

    def normalize_price(self, symbol: str, price: float) -> float:
        with self._read_lock:
            c = self._c()
            return float(c.price_to_precision(symbol, price))

    def market_order(self, symbol: str, side: str, amount: float, client_order_id: str,
                     stop_loss_price: float | None = None, take_profit_price: float | None = None,
                     reduce_only: bool = False) -> dict[str, Any]:
        with self._order_lock:
            c = self._c()
            params: dict[str, Any] = {"clientOrderId": client_order_id}
            if reduce_only:
                params["reduceOnly"] = True
            if stop_loss_price is not None:
                params["stopLoss"] = {"triggerPrice": stop_loss_price, "type": "market"}
            if take_profit_price is not None:
                params["takeProfit"] = {"triggerPrice": take_profit_price, "type": "market"}
            # Do not blindly retry market orders. A timeout can mean the exchange accepted the order.
            return c.create_order(symbol, "market", side, amount, None, params)

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        with self._read_lock:
            return self._c().fetch_order(order_id, symbol)

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._read_lock:
            return self._c().fetch_open_orders(symbol) if symbol else self._c().fetch_open_orders()

    def fetch_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._read_lock:
            c = self._c()
            if not c.has.get("fetchOrders"):
                return []
            return c.fetch_orders(symbol) if symbol else c.fetch_orders()

    def fetch_balance(self) -> dict[str, Any]:
        with self._read_lock:
            return self._c().fetch_balance()

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        with self._read_lock:
            c = self._c()
            if not c.has.get("fetchPositions"):
                # "Unsupported" is not the same as "flat". Returning [] lets callers
                # falsely confirm an emergency stop or reconcile a missing position as zero.
                raise RuntimeError(f"{self.config.exchange_id} adapter does not support authoritative open-position queries")
            rows = c.fetch_positions(symbols)
            if not isinstance(rows, list):
                raise RuntimeError(f"{self.config.exchange_id} returned a malformed open-position response")
            return rows

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        with self._cancel_lock:
            return self._c().cancel_order(order_id, symbol)

    def cancel_all_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Attempt every scoped cancellation and retain structured failures for the caller.

        A failed cancellation is not treated as proof an order remains open (the exchange
        may have accepted it before a timeout), nor is it silently discarded. Callers must
        re-fetch open orders and use that authoritative state before confirming a halt.
        """
        with self._cancel_lock:
            self.last_cancel_errors = []
            c = self._c()
            if c.has.get("cancelAllOrders"):
                try:
                    result = c.cancel_all_orders(symbol) if symbol else c.cancel_all_orders()
                except Exception as exc:
                    # A bulk endpoint error does not prove no orders remain. Record it and
                    # fall back to individual cancellations; callers still re-fetch open orders.
                    failure = {
                        "order_id": "",
                        "symbol": str(symbol or ""),
                        "error_type": type(exc).__name__,
                        "error": f"bulk cancel failed: {str(exc)[:900]}",
                    }
                    self.last_cancel_errors.append(failure)
                    logging.getLogger(__name__).warning(
                        "cancel_all_bulk_failed error_type=%s error=%s",
                        failure["error_type"], failure["error"], exc_info=True,
                    )
                else:
                    return result if isinstance(result, list) else [result]
            orders = self.fetch_open_orders(symbol)
            out = []
            for order in orders:
                oid = order.get("id")
                sym = order.get("symbol") or symbol
                if not oid or not sym:
                    self.last_cancel_errors.append({
                        "order_id": str(oid or ""),
                        "symbol": str(sym or ""),
                        "error_type": "InvalidOpenOrder",
                        "error": "open order is missing an order id or symbol; refusing to guess cancellation scope",
                    })
                    continue
                try:
                    out.append(c.cancel_order(oid, sym))
                except Exception as exc:
                    failure = {
                        "order_id": str(oid),
                        "symbol": str(sym),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                    }
                    self.last_cancel_errors.append(failure)
                    logging.getLogger(__name__).warning(
                        "cancel_all_order_failed order_id=%s symbol=%s error_type=%s error=%s",
                        failure["order_id"], failure["symbol"], failure["error_type"], failure["error"],
                        exc_info=True,
                    )
            return out

    def server_time(self) -> int | None:
        with self._read_lock:
            c = self._c()
            return c.fetch_time() if c.has.get("fetchTime") else None

    def feature_value(self, symbol: str, feature: str):
        with self._read_lock:
            c = self._c()
            return c.feature_value(symbol, "createOrder", feature)

    def supports_feature(self, symbol: str, feature: str) -> bool:
        # A false feature value means unsupported. Network/API errors must propagate:
        # an outage is not evidence that the exchange lacks the feature.
        return bool(self.feature_value(symbol, feature))

    def features(self, symbol: str) -> dict[str, Any]:
        with self._read_lock:
            c = self._c()
            return getattr(c, "features", {}) or {}
