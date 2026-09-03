from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx


LOG = logging.getLogger("paper-pilot")
NY = ZoneInfo("America/New_York")
PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
ACTIVE_ORDER_STATES = {"new", "accepted", "pending_new", "partially_filled", "held"}


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Config:
    api_key: str = field(default_factory=lambda: os.getenv("APCA_API_KEY_ID", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("APCA_API_SECRET_KEY", ""))
    orders_enabled: bool = field(default_factory=lambda: env_bool("PAPER_ORDERS_ENABLED", False))
    virtual_capital: float = field(default_factory=lambda: env_float("VIRTUAL_CAPITAL", 406.0))
    max_notional: float = field(default_factory=lambda: env_float("MAX_NOTIONAL", 75.0))
    max_daily_loss: float = field(default_factory=lambda: env_float("MAX_DAILY_LOSS", 4.06))
    max_trades: int = field(default_factory=lambda: env_int("MAX_TRADES_PER_DAY", 6))
    scan_seconds: int = field(default_factory=lambda: env_int("SCAN_SECONDS", 60))
    entry_start: time = time(9, 50)
    last_entry: time = time(15, 0)
    flatten_time: time = time(15, 45)
    stop_pct: float = field(default_factory=lambda: env_float("STOP_PCT", 0.0055))
    target_pct: float = field(default_factory=lambda: env_float("TARGET_PCT", 0.0100))
    min_volume_ratio: float = field(default_factory=lambda: env_float("MIN_VOLUME_RATIO", 1.50))
    cooldown_minutes: int = field(default_factory=lambda: env_int("COOLDOWN_MINUTES", 30))
    symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM", "SMH", "XLE")

    def validate(self) -> None:
        if self.max_notional > self.virtual_capital * 0.25:
            raise ValueError("MAX_NOTIONAL must be no more than 25% of VIRTUAL_CAPITAL")
        if self.max_daily_loss > self.virtual_capital * 0.01 + 1e-9:
            raise ValueError("MAX_DAILY_LOSS must be no more than 1% of VIRTUAL_CAPITAL")
        if self.max_trades > 6:
            raise ValueError("MAX_TRADES_PER_DAY cannot exceed 6 in the pilot")
        if self.target_pct / self.stop_pct < 1.5:
            raise ValueError("TARGET_PCT / STOP_PCT must be at least 1.5")
        # Missing credentials are handled as a fail-closed runtime state so the
        # health endpoint can come up before secrets are entered in Render.


class Alpaca:
    def __init__(self, config: Config):
        self.config = config
        self.http = httpx.AsyncClient(timeout=15, trust_env=False)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.config.api_key,
            "APCA-API-SECRET-KEY": self.config.api_secret,
        }

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.http.request(method, url, headers=self.headers, **kwargs)
        if response.is_error:
            LOG.error(
                "alpaca api error method=%s url=%s status=%s body=%s",
                method,
                url,
                response.status_code,
                response.text[:1000],
            )
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    async def account(self) -> dict[str, Any]:
        return await self._request("GET", f"{PAPER_TRADING_URL}/v2/account")

    async def clock(self) -> dict[str, Any]:
        return await self._request("GET", f"{PAPER_TRADING_URL}/v2/clock")

    async def positions(self) -> list[dict[str, Any]]:
        return await self._request("GET", f"{PAPER_TRADING_URL}/v2/positions")

    async def orders(self, status: str = "open", nested: bool = True) -> list[dict[str, Any]]:
        params = {"status": status, "nested": str(nested).lower(), "limit": 500, "direction": "desc"}
        return await self._request("GET", f"{PAPER_TRADING_URL}/v2/orders", params=params)

    async def today_orders(self) -> list[dict[str, Any]]:
        start = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)
        params = {
            "status": "all",
            "nested": "true",
            "limit": 500,
            "direction": "desc",
            "after": start.astimezone(timezone.utc).isoformat(),
        }
        return await self._request("GET", f"{PAPER_TRADING_URL}/v2/orders", params=params)

    async def bars(self, start: datetime, end: datetime) -> dict[str, list[dict[str, Any]]]:
        params = {
            "symbols": ",".join(self.config.symbols),
            "timeframe": "1Min",
            "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
            "limit": 1000,
            "adjustment": "raw",
            "feed": "iex",
            "sort": "asc",
        }
        data = await self._request("GET", f"{DATA_URL}/v2/stocks/bars", params=params)
        return data.get("bars", {})

    async def submit_bracket(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"{PAPER_TRADING_URL}/v2/orders", json=payload)

    async def cancel_order(self, order_id: str) -> None:
        await self._request("DELETE", f"{PAPER_TRADING_URL}/v2/orders/{order_id}")

    async def submit_flatten(self, symbol: str, qty: float, client_order_id: str) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "qty": f"{qty:.6f}",
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "client_order_id": client_order_id,
        }
        return await self._request("POST", f"{PAPER_TRADING_URL}/v2/orders", json=payload)


def ema(values: list[float], length: int) -> float:
    multiplier = 2 / (length + 1)
    result = values[0]
    for value in values[1:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def vwap(bars: list[dict[str, Any]]) -> float:
    weighted = sum(float(bar["c"]) * float(bar["v"]) for bar in bars)
    volume = sum(float(bar["v"]) for bar in bars)
    return weighted / volume if volume else 0.0


def tick(price: float) -> float:
    return round(price + 1e-9, 2)


def analyze(symbol: str, bars: list[dict[str, Any]], config: Config) -> dict[str, Any] | None:
    if len(bars) < 30:
        return None
    closes = [float(bar["c"]) for bar in bars]
    volumes = [float(bar["v"]) for bar in bars]
    last = closes[-1]
    prior_high = max(float(bar["h"]) for bar in bars[-16:-1])
    base_volume = statistics.median(volumes[-21:-1]) or 1.0
    volume_ratio = volumes[-1] / base_volume
    session_vwap = vwap(bars)
    ema9 = ema(closes[-30:], 9)
    ema20 = ema(closes[-30:], 20)
    breakout_pct = (last / prior_high) - 1
    if not (last > session_vwap and ema9 > ema20):
        return None
    if not (0 <= breakout_pct <= 0.0020):
        return None
    if volume_ratio < config.min_volume_ratio:
        return None
    return {
        "symbol": symbol,
        "price": last,
        "vwap": session_vwap,
        "ema9": ema9,
        "ema20": ema20,
        "volume_ratio": volume_ratio,
        "breakout_pct": breakout_pct,
    }


class PaperPilot:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.config.validate()
        self.alpaca = Alpaca(self.config)
        self._stop = asyncio.Event()
        self._last_entry_at: datetime | None = None
        self._last_scan_at: str | None = None
        self._last_signal: dict[str, Any] | None = None
        self._last_order: dict[str, Any] | None = None
        self._paused_reason: str | None = None
        self._last_error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def public_status(self) -> dict[str, Any]:
        return {
            "mode": "paper-only",
            "orders_enabled": self.config.orders_enabled,
            "virtual_capital": self.config.virtual_capital,
            "max_notional": self.config.max_notional,
            "max_daily_loss": self.config.max_daily_loss,
            "max_trades_per_day": self.config.max_trades,
            "symbols": self.config.symbols,
            "last_scan_at": self._last_scan_at,
            "last_signal": self._last_signal,
            "last_order": self._last_order,
            "paused_reason": self._paused_reason,
            "last_error": self._last_error,
        }

    async def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.scan_once()
                    self._last_error = None
                except Exception as exc:  # keep service alive but fail closed
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._paused_reason = "error_fail_closed"
                    LOG.exception("scan failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.scan_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.alpaca.close()

    async def scan_once(self) -> None:
        now = datetime.now(NY)
        self._last_scan_at = now.isoformat()
        self._last_signal = None

        if not self.config.api_key or not self.config.api_secret:
            self._paused_reason = "paper_credentials_missing"
            return

        account, clock, positions, open_orders, today_orders = await asyncio.gather(
            self.alpaca.account(),
            self.alpaca.clock(),
            self.alpaca.positions(),
            self.alpaca.orders("open", True),
            self.alpaca.today_orders(),
        )
        if not account.get("trading_blocked") is False:
            self._paused_reason = "alpaca_account_blocked"
            return
        if not clock.get("is_open"):
            self._paused_reason = "market_closed"
            return

        current_time = now.time().replace(tzinfo=None)
        tagged = self._tagged_parents(today_orders, now)
        if current_time >= self.config.flatten_time:
            await self._flatten_tagged(tagged, positions, open_orders)
            self._paused_reason = "flatten_window"
            return
        if current_time < self.config.entry_start or current_time > self.config.last_entry:
            self._paused_reason = "outside_entry_window"
            return
        if positions or open_orders:
            self._paused_reason = "account_not_empty"
            return
        if len(tagged) >= self.config.max_trades:
            self._paused_reason = "daily_trade_limit"
            return
        pnl = self._tagged_pnl(tagged, positions)
        if pnl <= -self.config.max_daily_loss:
            self._paused_reason = "daily_loss_limit"
            return
        if self._last_entry_at and now - self._last_entry_at < timedelta(minutes=self.config.cooldown_minutes):
            self._paused_reason = "cooldown"
            return

        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        bars_by_symbol = await self.alpaca.bars(start, now)
        if not bars_by_symbol:
            self._paused_reason = "market_data_unavailable"
            return

        above_vwap = 0
        candidates: list[dict[str, Any]] = []
        for symbol in self.config.symbols:
            bars = bars_by_symbol.get(symbol, [])
            if len(bars) >= 20 and float(bars[-1]["c"]) > vwap(bars):
                above_vwap += 1
            candidate = analyze(symbol, bars, self.config)
            if candidate:
                candidates.append(candidate)
        if above_vwap < 4:
            self._paused_reason = f"weak_breadth_{above_vwap}_of_{len(self.config.symbols)}"
            return
        if not candidates:
            self._paused_reason = "no_confirmed_breakout"
            return

        # Alpaca does not accept fractional quantities for advanced bracket
        # orders. Keep the pilot protected by using whole shares only and skip
        # any candidate that cannot fit inside the configured notional cap.
        affordable = [
            item
            for item in candidates
            if tick(float(item["price"]) * 1.0005) <= self.config.max_notional
        ]
        if not affordable:
            self._paused_reason = "no_affordable_whole_share_candidate"
            return

        signal = max(affordable, key=lambda item: item["volume_ratio"])
        self._last_signal = signal
        if not self.config.orders_enabled:
            self._paused_reason = "dry_run_signal_only"
            return

        payload = self._order_payload(signal, now)
        order = await self.alpaca.submit_bracket(payload)
        self._last_entry_at = now
        self._last_order = {
            "id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
            "symbol": order.get("symbol"),
            "status": order.get("status"),
            "submitted_at": order.get("submitted_at"),
        }
        self._paused_reason = "order_submitted"

    def _order_payload(self, signal: dict[str, Any], now: datetime) -> dict[str, Any]:
        price = float(signal["price"])
        limit_price = tick(price * 1.0005)
        stop_price = tick(limit_price * (1 - self.config.stop_pct))
        target_price = tick(limit_price * (1 + self.config.target_pct))
        qty = math.floor(self.config.max_notional / limit_price)
        if qty <= 0 or qty * limit_price > self.config.max_notional + 0.01:
            raise ValueError("calculated quantity violates notional cap")
        planned_loss = qty * (limit_price - stop_price)
        if planned_loss > self.config.max_daily_loss / 2:
            raise ValueError("planned trade loss exceeds half the daily loss limit")
        tag = now.strftime("eli406-%Y%m%d-%H%M%S")
        return {
            "symbol": signal["symbol"],
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{limit_price:.2f}",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{target_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
            "extended_hours": False,
            "client_order_id": tag,
        }

    def _tagged_parents(self, orders: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        prefix = now.strftime("eli406-%Y%m%d-")
        return [order for order in orders if str(order.get("client_order_id", "")).startswith(prefix)]

    def _tagged_pnl(self, tagged: list[dict[str, Any]], positions: list[dict[str, Any]]) -> float:
        pnl = 0.0
        for order in tagged:
            if order.get("status") != "filled" or not order.get("filled_avg_price"):
                continue
            entry = float(order["filled_avg_price"])
            for leg in order.get("legs") or []:
                if leg.get("status") == "filled" and leg.get("filled_avg_price"):
                    pnl += (float(leg["filled_avg_price"]) - entry) * float(leg["filled_qty"])
        tagged_symbols = {order.get("symbol") for order in tagged}
        for position in positions:
            if position.get("symbol") in tagged_symbols:
                pnl += float(position.get("unrealized_pl", 0))
        return pnl

    async def _flatten_tagged(
        self,
        tagged: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
    ) -> None:
        tagged_ids: set[str] = set()
        remaining_by_symbol: dict[str, float] = {}
        for order in tagged:
            tagged_ids.add(str(order.get("id")))
            symbol = str(order.get("symbol"))
            filled_entry = float(order.get("filled_qty") or 0)
            filled_exits = 0.0
            for leg in order.get("legs") or []:
                tagged_ids.add(str(leg.get("id")))
                if leg.get("side") == "sell":
                    filled_exits += float(leg.get("filled_qty") or 0)
            remaining_by_symbol[symbol] = remaining_by_symbol.get(symbol, 0.0) + max(
                0.0, filled_entry - filled_exits
            )
        exit_prefix = datetime.now(NY).strftime("eli406exit-%Y%m%d-")
        open_exit_symbols = {
            str(order.get("symbol"))
            for order in open_orders
            if str(order.get("client_order_id", "")).startswith(exit_prefix)
        }
        for order in open_orders:
            if str(order.get("id")) in tagged_ids:
                try:
                    await self.alpaca.cancel_order(str(order["id"]))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 422:
                        raise
        if tagged_ids:
            await asyncio.sleep(0.5)
        for position in positions:
            symbol = str(position.get("symbol"))
            if symbol in open_exit_symbols:
                continue
            bot_qty = min(remaining_by_symbol.get(symbol, 0.0), float(position.get("qty") or 0))
            if bot_qty > 0:
                client_id = datetime.now(NY).strftime(f"eli406exit-%Y%m%d-{symbol}-%H%M%S")
                await self.alpaca.submit_flatten(symbol, bot_qty, client_id)
