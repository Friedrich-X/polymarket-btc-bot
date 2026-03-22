"""
Momentum Trading Strategy for Gabagool Bot.

Detects BTC price moves on Binance (via BinanceFeed callback) and buys
the corresponding Polymarket token before the market reprices.

  - BTC moves UP   -> buy YES token
  - BTC moves DOWN -> buy NO token
"""

import time
from typing import Any

from loguru import logger


class MomentumStrategy:
    """
    Reacts to BinanceFeed on_signal callbacks by placing directional
    trades on Polymarket BTC 15-min markets.

    Designed to be wired as::

        strategy = MomentumStrategy(poly_client, trade_logger, config)
        feed = BinanceFeed(on_signal=strategy.on_signal, ...)
    """

    def __init__(
        self,
        poly_client,
        trade_logger,
        config: dict,
        paper_mode: bool = True,
    ) -> None:
        """
        Args:
            poly_client: GabagoolPolyClient instance (connected or inert).
            trade_logger: TradeLogger instance for recording trades.
            config: Momentum config dict, expected keys:
                - entry_min_delta  (float): minimum |delta| to trigger a trade.
                - cooldown_secs    (int):   seconds between trades.
                - order_size_usd   (float): USD per trade.
                - max_trades_per_window (int): cap per 15-min window.
            paper_mode: When True, simulate fills instead of placing live orders.
        """
        self._poly = poly_client
        self._logger = trade_logger
        self._paper_mode = paper_mode

        # Config with defaults
        self._entry_min_delta: float = config.get("entry_min_delta", 0.003)
        self._cooldown_secs: int = config.get("cooldown_secs", 30)
        self._order_size_usd: float = config.get("order_size_usd", 5.0)
        self._max_trades_per_window: int = config.get("max_trades_per_window", 5)

        # State
        self._last_trade_ts: float = 0.0
        self._trades_this_window: int = 0
        self._total_trades: int = 0
        self._total_signals: int = 0
        self._skipped_cooldown: int = 0
        self._skipped_window_limit: int = 0
        self._skipped_below_threshold: int = 0
        self._skipped_no_market: int = 0
        self._skipped_execution_fail: int = 0

        mode_label = "PAPER" if paper_mode else "LIVE"
        logger.info(
            "MomentumStrategy initialised | mode={} min_delta={} cooldown={}s "
            "size=${} max_trades/window={}",
            mode_label,
            self._entry_min_delta,
            self._cooldown_secs,
            self._order_size_usd,
            self._max_trades_per_window,
        )

    # ------------------------------------------------------------------
    # Signal handler (entry point from BinanceFeed)
    # ------------------------------------------------------------------

    def on_signal(self, delta: float, btc_price: float, timestamp: float) -> None:
        """
        Called by BinanceFeed when BTC price move exceeds the feed threshold.

        Decides whether to trade, determines direction, and executes.

        Args:
            delta: Fractional price change (e.g. 0.005 = +0.5%).
            btc_price: Current BTC price in USD.
            timestamp: Unix timestamp of the triggering trade.
        """
        self._total_signals += 1

        # --- Gate 1: delta large enough for our strategy threshold ---
        if abs(delta) < self._entry_min_delta:
            self._skipped_below_threshold += 1
            logger.debug(
                "Signal below entry threshold | delta={:.5f} threshold={:.5f}",
                delta,
                self._entry_min_delta,
            )
            return

        direction = "YES" if delta > 0 else "NO"

        logger.info(
            "Signal received | delta={:+.5f} btc=${:,.2f} direction={} ts={:.3f}",
            delta,
            btc_price,
            direction,
            timestamp,
        )

        # --- Gate 2: cooldown and window limits ---
        if not self._should_trade():
            return

        # --- Execute ---
        self._execute_trade(direction, btc_price)

    # ------------------------------------------------------------------
    # Trade gating
    # ------------------------------------------------------------------

    def _should_trade(self) -> bool:
        """
        Check whether a new trade is allowed based on:
        - cooldown timer since the last trade
        - max trades per 15-min window
        """
        now = time.time()

        # Cooldown check
        elapsed = now - self._last_trade_ts
        if elapsed < self._cooldown_secs:
            self._skipped_cooldown += 1
            logger.info(
                "Trade skipped (cooldown) | {:.1f}s remaining",
                self._cooldown_secs - elapsed,
            )
            return False

        # Window trade limit
        if self._trades_this_window >= self._max_trades_per_window:
            self._skipped_window_limit += 1
            logger.info(
                "Trade skipped (window limit) | {}/{} trades used",
                self._trades_this_window,
                self._max_trades_per_window,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_trade(self, direction: str, btc_price: float) -> None:
        """
        Place a trade on Polymarket.

        In live mode:  calls poly_client.place_market_buy().
        In paper mode: estimates fill price from the order book and logs it.

        Args:
            direction: "YES" or "NO".
            btc_price: BTC price that triggered the signal.
        """
        # Discover the current 15-min market
        market = self._poly.discover_current_market()
        if market is None:
            self._skipped_no_market += 1
            logger.warning("No active market found — skipping trade")
            return

        token_id = (
            market["yes_token_id"] if direction == "YES" else market["no_token_id"]
        )
        market_slug = market["slug"]

        if self._paper_mode:
            self._execute_paper(direction, token_id, market_slug, btc_price)
        else:
            self._execute_live(direction, token_id, market_slug, btc_price)

    def _execute_live(
        self,
        direction: str,
        token_id: str,
        market_slug: str,
        btc_price: float,
    ) -> None:
        """Place a FOK market buy on Polymarket and record the trade."""
        resp = self._poly.place_market_buy(token_id, self._order_size_usd)

        if resp is None:
            self._skipped_execution_fail += 1
            logger.error(
                "Live order failed | direction={} token={} size=${}",
                direction,
                token_id[:12] + "...",
                self._order_size_usd,
            )
            return

        # Estimate fill price from order size (tokens ~= cost / price, price ~= ask)
        # The API response doesn't always give us a fill price directly,
        # so we use the best ask as an approximation for logging.
        prices = self._poly.get_best_prices()
        ask_key = "yes_ask" if direction == "YES" else "no_ask"
        fill_price = prices.get(ask_key) or 0.50
        quantity = self._order_size_usd / fill_price if fill_price > 0 else 0.0

        self._record_and_update(
            direction=direction,
            fill_price=fill_price,
            quantity=quantity,
            market_slug=market_slug,
            btc_price=btc_price,
            is_paper=False,
        )

        logger.info(
            "LIVE trade executed | direction={} price={:.4f} qty={:.2f} cost=${:.2f}",
            direction,
            fill_price,
            quantity,
            self._order_size_usd,
        )

    def _execute_paper(
        self,
        direction: str,
        token_id: str,
        market_slug: str,
        btc_price: float,
    ) -> None:
        """Simulate a fill using current orderbook prices and record the trade."""
        prices = self._poly.get_best_prices()
        ask_key = "yes_ask" if direction == "YES" else "no_ask"
        fill_price = prices.get(ask_key)

        if fill_price is None:
            self._skipped_execution_fail += 1
            logger.warning(
                "Paper trade skipped — no ask price available for {} token",
                direction,
            )
            return

        quantity = self._order_size_usd / fill_price if fill_price > 0 else 0.0

        self._record_and_update(
            direction=direction,
            fill_price=fill_price,
            quantity=quantity,
            market_slug=market_slug,
            btc_price=btc_price,
            is_paper=True,
        )

        logger.info(
            "PAPER trade executed | direction={} price={:.4f} qty={:.2f} cost=${:.2f}",
            direction,
            fill_price,
            quantity,
            self._order_size_usd,
        )

    def _record_and_update(
        self,
        direction: str,
        fill_price: float,
        quantity: float,
        market_slug: str,
        btc_price: float,
        is_paper: bool,
    ) -> None:
        """Record trade via TradeLogger and update internal counters."""
        self._logger.record_trade(
            strategy="momentum",
            token_side=direction,
            price=fill_price,
            quantity=quantity,
            cost_usd=self._order_size_usd,
            market_slug=market_slug,
            is_paper=is_paper,
        )

        now = time.time()
        self._last_trade_ts = now
        self._trades_this_window += 1
        self._total_trades += 1

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------

    def reset_window(self) -> None:
        """Reset the trade counter for a new 15-min window."""
        prev = self._trades_this_window
        self._trades_this_window = 0
        logger.info(
            "Momentum window reset | trades in previous window: {}", prev
        )

    # ------------------------------------------------------------------
    # Stats / introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return strategy statistics for monitoring / display."""
        return {
            "strategy": "momentum",
            "paper_mode": self._paper_mode,
            "total_signals": self._total_signals,
            "total_trades": self._total_trades,
            "trades_this_window": self._trades_this_window,
            "max_trades_per_window": self._max_trades_per_window,
            "cooldown_secs": self._cooldown_secs,
            "entry_min_delta": self._entry_min_delta,
            "order_size_usd": self._order_size_usd,
            "skipped": {
                "cooldown": self._skipped_cooldown,
                "window_limit": self._skipped_window_limit,
                "below_threshold": self._skipped_below_threshold,
                "no_market": self._skipped_no_market,
                "execution_fail": self._skipped_execution_fail,
            },
        }
