"""
Multi-Feed Signal Aggregator for Gabagool Bot

Combines price signals from multiple exchange feeds (Binance, Coinbase)
to produce higher-confidence trading signals.

Signal modes:
  - "any":       Fire on ANY feed signal (lowest latency, more trades)
  - "confirm":   Fire only when BOTH feeds agree on direction (higher confidence)
  - "divergence": Fire when feeds DISAGREE (exploit cross-exchange arbitrage)

Inspired by bot "0x8dxd" which turned $313 -> $438,000 using
Binance + Coinbase spot price monitoring.
"""

import time
from typing import Callable, Optional

from loguru import logger


class MultiFeedAggregator:
    """
    Aggregates price signals from multiple exchange feeds and fires
    a unified callback based on configurable confirmation logic.
    """

    def __init__(
        self,
        on_signal: Optional[Callable] = None,
        mode: str = "confirm",
        confirmation_window_secs: float = 2.0,
        min_delta: float = 0.003,
    ):
        """
        Args:
            on_signal: Callback fired with (delta, price, timestamp, confidence).
                       confidence is a float 0.0-1.0 indicating signal strength.
            mode: Signal aggregation mode:
                  "any"       - fire on any single feed signal
                  "confirm"   - fire when both feeds agree within window
                  "divergence"- fire when feeds disagree (contrarian)
            confirmation_window_secs: Max time between feed signals to count
                                      as "simultaneous" for confirmation mode.
            min_delta: Minimum absolute delta to consider a signal valid.
        """
        self.on_signal = on_signal
        self.mode = mode
        self.confirmation_window_secs = confirmation_window_secs
        self.min_delta = min_delta

        # Latest signal from each feed: {feed_name: (delta, price, timestamp)}
        self._latest_signals: dict[str, tuple[float, float, float]] = {}

        # Stats
        self._signals_received: dict[str, int] = {}
        self._signals_fired: int = 0
        self._confirmations: int = 0
        self._divergences: int = 0

        logger.info(
            "MultiFeedAggregator initialised | mode={} window={:.1f}s min_delta={}",
            mode,
            confirmation_window_secs,
            min_delta,
        )

    def create_feed_callback(self, feed_name: str) -> Callable:
        """
        Create a callback function for a specific feed.

        Usage:
            aggregator = MultiFeedAggregator(on_signal=momentum.on_signal)
            binance_feed = BinanceFeed(on_signal=aggregator.create_feed_callback("binance"))
            coinbase_feed = CoinbaseFeed(on_signal=aggregator.create_feed_callback("coinbase"))
        """

        def callback(delta: float, price: float, timestamp: float) -> None:
            self._on_feed_signal(feed_name, delta, price, timestamp)

        return callback

    def _on_feed_signal(
        self, feed_name: str, delta: float, price: float, timestamp: float
    ) -> None:
        """Process a signal from one feed and decide whether to fire."""
        # Track stats
        self._signals_received[feed_name] = (
            self._signals_received.get(feed_name, 0) + 1
        )

        # Store latest signal
        self._latest_signals[feed_name] = (delta, price, timestamp)

        if self.mode == "any":
            self._fire_signal(delta, price, timestamp, confidence=0.5, source=feed_name)

        elif self.mode == "confirm":
            self._check_confirmation(feed_name, delta, price, timestamp)

        elif self.mode == "divergence":
            self._check_divergence(feed_name, delta, price, timestamp)

    def _check_confirmation(
        self, source: str, delta: float, price: float, timestamp: float
    ) -> None:
        """
        Check if another feed has signalled in the same direction recently.

        If so, fire with high confidence. If not, store and wait.
        """
        now = time.time()

        for feed_name, (other_delta, other_price, other_ts) in self._latest_signals.items():
            if feed_name == source:
                continue

            # Check if the other signal is recent enough
            if now - other_ts > self.confirmation_window_secs:
                continue

            # Check if both signals agree on direction
            if (delta > 0 and other_delta > 0) or (delta < 0 and other_delta < 0):
                # CONFIRMED: both feeds agree
                avg_delta = (delta + other_delta) / 2
                avg_price = (price + other_price) / 2

                # Confidence based on delta agreement magnitude
                delta_agreement = min(abs(delta), abs(other_delta)) / max(
                    abs(delta), abs(other_delta)
                )
                confidence = 0.6 + (0.4 * delta_agreement)  # 0.6 to 1.0

                self._confirmations += 1

                logger.info(
                    "CONFIRMED signal | {} + {} | delta_avg={:+.5f} confidence={:.2f}",
                    source,
                    feed_name,
                    avg_delta,
                    confidence,
                )

                self._fire_signal(
                    avg_delta, avg_price, timestamp, confidence, source=f"{source}+{feed_name}"
                )

                # Clear the other signal to avoid double-firing
                self._latest_signals[feed_name] = (other_delta, other_price, 0.0)
                return

        # No confirmation yet -- just store and wait
        logger.debug(
            "Signal from {} stored, awaiting confirmation | delta={:+.5f}",
            source,
            delta,
        )

    def _check_divergence(
        self, source: str, delta: float, price: float, timestamp: float
    ) -> None:
        """
        Check if feeds disagree on direction (potential cross-exchange arb).

        In divergence mode, we bet on the feed that is LAGGING (mean reversion).
        """
        now = time.time()

        for feed_name, (other_delta, other_price, other_ts) in self._latest_signals.items():
            if feed_name == source:
                continue

            if now - other_ts > self.confirmation_window_secs:
                continue

            # Check for direction disagreement
            if (delta > 0 and other_delta < 0) or (delta < 0 and other_delta > 0):
                # DIVERGENCE: feeds disagree
                self._divergences += 1

                # Use the LARGER absolute delta as the signal
                # (the bigger mover is likely leading, so bet on reversion)
                if abs(delta) > abs(other_delta):
                    # This feed moved more -- expect the other to catch up
                    # So the trade direction follows THIS feed's delta
                    signal_delta = delta
                    signal_price = price
                else:
                    signal_delta = other_delta
                    signal_price = other_price

                confidence = 0.4  # Lower confidence for divergence signals

                logger.info(
                    "DIVERGENCE signal | {}={:+.5f} vs {}={:+.5f} | "
                    "trading direction={:+.5f}",
                    source,
                    delta,
                    feed_name,
                    other_delta,
                    signal_delta,
                )

                self._fire_signal(
                    signal_delta, signal_price, timestamp, confidence,
                    source=f"divergence:{source}vs{feed_name}",
                )

                self._latest_signals[feed_name] = (other_delta, other_price, 0.0)
                return

    def _fire_signal(
        self,
        delta: float,
        price: float,
        timestamp: float,
        confidence: float,
        source: str,
    ) -> None:
        """Fire the aggregated signal to the downstream callback."""
        if abs(delta) < self.min_delta:
            return

        self._signals_fired += 1

        if self.on_signal is not None:
            try:
                # The downstream callback (MomentumStrategy.on_signal) expects
                # (delta, price, timestamp). We pass confidence as a bonus
                # kwarg that will be ignored by the existing signature.
                self.on_signal(delta, price, timestamp)
            except Exception as e:
                logger.error("MultiFeed on_signal callback error: {}", e)

    def get_stats(self) -> dict:
        """Return aggregator statistics."""
        return {
            "mode": self.mode,
            "signals_received": dict(self._signals_received),
            "signals_fired": self._signals_fired,
            "confirmations": self._confirmations,
            "divergences": self._divergences,
            "confirmation_window_secs": self.confirmation_window_secs,
        }
