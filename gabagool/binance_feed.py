"""
Binance aggTrade WebSocket feed for real-time BTC price deltas.

Connects to Binance's aggTrade stream, maintains a rolling price window,
and fires a callback when price movement exceeds a threshold.
"""

import asyncio
import json
import time
from collections import deque
from typing import Callable, Optional

import websockets
from loguru import logger


class BinanceFeed:
    """
    Real-time BTC price feed from Binance aggTrade WebSocket.

    Maintains a rolling window of (timestamp, price) for delta calculation.
    Fires on_signal callback when |price_delta| exceeds min_delta.
    """

    WS_URL = "wss://stream.binance.com:9443/ws"

    def __init__(
        self,
        symbol: str = "btcusdt",
        on_signal: Optional[Callable] = None,
        min_delta: float = 0.003,
        lookback_secs: float = 3,
    ):
        self.symbol = symbol.lower()
        self.on_signal = on_signal
        self.min_delta = min_delta
        self.lookback_secs = lookback_secs

        # Rolling price window: deque of (timestamp_secs, price_float)
        # Keep up to 60s of data to allow flexible lookback queries
        self._max_history_secs = max(60.0, lookback_secs * 3)
        self._prices: deque[tuple[float, float]] = deque()

        self._latest_price: Optional[float] = None
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

    async def start(self) -> None:
        """Connect and start receiving trades. Reconnects on failure."""
        self._running = True
        backoff = 1.0

        while self._running:
            try:
                url = f"{self.WS_URL}/{self.symbol}@aggTrade"
                logger.info(f"Connecting to Binance aggTrade stream: {url}")

                async with websockets.connect(url) as ws:
                    self._ws = ws
                    backoff = 1.0  # Reset backoff on successful connect
                    logger.info(f"Connected to Binance {self.symbol} aggTrade stream")

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        self._handle_message(raw_msg)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Binance WebSocket closed: {e}")
            except Exception as e:
                logger.error(f"Binance WebSocket error: {e}")

            if not self._running:
                break

            # Exponential backoff: 1s, 2s, 4s, 8s, ... capped at 30s
            logger.info(f"Reconnecting in {backoff:.0f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

        self._ws = None
        logger.info("Binance feed stopped")

    async def stop(self) -> None:
        """Clean shutdown."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        logger.info("Binance feed shutdown requested")

    def get_latest_price(self) -> Optional[float]:
        """Get most recent trade price."""
        return self._latest_price

    def price_delta(self, lookback_secs: Optional[float] = None) -> Optional[float]:
        """
        Calculate price change as a fraction over the lookback window.

        Returns:
            Fractional price change (e.g., 0.003 = 0.3%), or None if
            insufficient data in the window.
        """
        if not self._prices or self._latest_price is None:
            return None

        lookback = lookback_secs if lookback_secs is not None else self.lookback_secs
        cutoff = time.time() - lookback

        # Find the oldest price within the lookback window
        old_price = None
        for ts, price in self._prices:
            if ts >= cutoff:
                old_price = price
                break

        if old_price is None or old_price == 0:
            return None

        return (self._latest_price - old_price) / old_price

    def _handle_message(self, raw_msg: str) -> None:
        """Parse aggTrade message and update state."""
        try:
            data = json.loads(raw_msg)
            price = float(data["p"])
            ts_ms = data["T"]
            ts_secs = ts_ms / 1000.0
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse aggTrade message: {e}")
            return

        self._latest_price = price
        self._prices.append((ts_secs, price))
        self._cleanup_old_prices()

        # Check signal threshold
        delta = self.price_delta()
        if delta is not None and abs(delta) >= self.min_delta:
            if self.on_signal is not None:
                try:
                    self.on_signal(delta, price, ts_secs)
                except Exception as e:
                    logger.error(f"on_signal callback error: {e}")

    def _cleanup_old_prices(self) -> None:
        """Remove prices older than max history window."""
        cutoff = time.time() - self._max_history_secs
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()
