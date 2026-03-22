"""
Coinbase WebSocket feed for real-time BTC price data.

Connects to Coinbase Advanced Trade WebSocket (public, no auth required),
subscribes to the ticker channel for BTC-USD, and maintains a rolling
price window identical to BinanceFeed.

Used as a secondary data source alongside BinanceFeed for:
  - Price confirmation (both exchanges agree on direction)
  - Divergence detection (one exchange leads the other)
  - Redundancy (if one feed drops, the other continues)
"""

import asyncio
import json
import time
from collections import deque
from typing import Callable, Optional

import websockets
from loguru import logger


class CoinbaseFeed:
    """
    Real-time BTC price feed from Coinbase WebSocket.

    Mirrors the BinanceFeed interface so both can be used interchangeably
    by the signal aggregator.
    """

    WS_URL = "wss://ws-feed.exchange.coinbase.com"

    def __init__(
        self,
        product_id: str = "BTC-USD",
        on_signal: Optional[Callable] = None,
        min_delta: float = 0.003,
        lookback_secs: float = 3,
    ):
        self.product_id = product_id
        self.on_signal = on_signal
        self.min_delta = min_delta
        self.lookback_secs = lookback_secs

        # Rolling price window: deque of (timestamp_secs, price_float)
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
                logger.info("Connecting to Coinbase WebSocket: {}", self.WS_URL)

                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    backoff = 1.0

                    # Subscribe to the ticker channel (real-time price updates)
                    subscribe_msg = {
                        "type": "subscribe",
                        "product_ids": [self.product_id],
                        "channels": ["ticker"],
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(
                        "Subscribed to Coinbase {} ticker channel",
                        self.product_id,
                    )

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        self._handle_message(raw_msg)

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning("Coinbase WebSocket closed: {}", e)
            except Exception as e:
                logger.error("Coinbase WebSocket error: {}", e)

            if not self._running:
                break

            logger.info("Coinbase reconnecting in {:.0f}s...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

        self._ws = None
        logger.info("Coinbase feed stopped")

    async def stop(self) -> None:
        """Clean shutdown."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        logger.info("Coinbase feed shutdown requested")

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

        old_price = None
        for ts, price in self._prices:
            if ts >= cutoff:
                old_price = price
                break

        if old_price is None or old_price == 0:
            return None

        return (self._latest_price - old_price) / old_price

    def _handle_message(self, raw_msg: str) -> None:
        """Parse Coinbase ticker message and update state."""
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse Coinbase message: {}", e)
            return

        msg_type = data.get("type", "")

        # Skip subscription confirmations and heartbeats
        if msg_type in ("subscriptions", "heartbeat", "error"):
            if msg_type == "error":
                logger.warning("Coinbase error: {}", data.get("message", "unknown"))
            return

        # We want "ticker" messages which contain the latest trade price
        if msg_type != "ticker":
            return

        try:
            price = float(data["price"])
            # Coinbase provides ISO timestamp; convert to unix seconds
            # For low-latency, use local time instead of parsing ISO
            ts_secs = time.time()
        except (KeyError, ValueError) as e:
            logger.warning("Failed to extract price from Coinbase ticker: {}", e)
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
                    logger.error("Coinbase on_signal callback error: {}", e)

    def _cleanup_old_prices(self) -> None:
        """Remove prices older than max history window."""
        cutoff = time.time() - self._max_history_secs
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()
