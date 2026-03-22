"""
Gabagool Bot -- Main Orchestrator

Event-driven Polymarket BTC 15-minute trading bot.
Runs two strategies in parallel:
  1. Momentum: reacts to Binance BTC price moves via WebSocket
  2. Spread Capture: polls Polymarket orderbook for arbitrage

Manages the 15-minute market lifecycle: discover, trade, resolve, repeat.
"""

import asyncio
import argparse
import json
import math
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from dotenv import load_dotenv

from gabagool.binance_feed import BinanceFeed
from gabagool.poly_client import GabagoolPolyClient
from gabagool.paper_engine import PaperEngine
from gabagool.strategies.momentum import MomentumStrategy
from gabagool.strategies.spread_capture import SpreadCaptureStrategy
from gabagool.trade_logger import TradeLogger


MARKET_INTERVAL_SECS = 300  # 5 minutes (default, loaded from config)


def _seconds_until_next_boundary(interval: int = MARKET_INTERVAL_SECS) -> float:
    """Calculate seconds until the next market clock boundary."""
    now = time.time()
    next_boundary = math.ceil(now / interval) * interval
    remaining = next_boundary - now
    # If we are exactly on a boundary, the next one is one interval away
    if remaining < 1.0:
        remaining = interval
    return remaining


class GabagoolBot:
    """
    Main orchestrator that ties Binance feed, Polymarket client,
    strategies, and trade logging into a single event loop.
    """

    def __init__(self, config: dict, live: bool = False, budget: float = 60.0) -> None:
        self.config = config
        self.live = live
        self.budget = budget
        self._running = False
        self._start_time: float = 0.0

        # Components (initialised in start())
        self._poly_client: GabagoolPolyClient | None = None
        self._paper_engine: PaperEngine | None = None
        self._trade_logger: TradeLogger | None = None
        self._binance_feed: BinanceFeed | None = None
        self._momentum: MomentumStrategy | None = None
        self._spread_capture: SpreadCaptureStrategy | None = None

        # The trading client used by strategies: PaperEngine in paper mode,
        # GabagoolPolyClient in live mode.
        self._trading_client: GabagoolPolyClient | PaperEngine | None = None

        # Async tasks
        self._binance_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._transition_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Main entry point. Connect, discover, and run the event loop."""
        self._running = True
        self._start_time = time.time()
        self._print_banner()

        # 1. Initialise trade logger
        log_file = self.config.get("general", {}).get("log_file", "gabagool_trades.json")
        self._trade_logger = TradeLogger(log_file=log_file)

        # 2. Initialise Polymarket client (always needed for real price data)
        interval = self._config.get("general", {}).get("market_interval_secs", 300)
        self._poly_client = GabagoolPolyClient(live=self.live, interval_secs=interval)
        if not self.live:
            # In paper mode the client still needs to connect for read-only
            # operations (market discovery, orderbook). connect() requires
            # credentials which may not be available in pure paper mode,
            # so we connect and let PaperEngine delegate reads.
            try:
                self._poly_client.connect()
            except RuntimeError as exc:
                logger.warning(
                    "Could not connect poly_client (credentials missing?): {}. "
                    "Continuing in paper mode -- orderbook data will be unavailable.",
                    exc,
                )

        # 3. Set up trading client (paper or live)
        if self.live:
            self._trading_client = self._poly_client
            logger.info("Trading mode: LIVE")
        else:
            self._paper_engine = PaperEngine(
                poly_client=self._poly_client,
                trade_logger=self._trade_logger,
                initial_balance=self.budget,
            )
            self._trading_client = self._paper_engine
            logger.info("Trading mode: PAPER (balance=${:.2f})", self.budget)

        # 4. Discover the current market
        market = await asyncio.to_thread(self._trading_client.discover_current_market)
        if market:
            logger.info(
                "Current market: {} | YES={} | NO={}",
                market["slug"],
                market["yes_token_id"][:12] + "...",
                market["no_token_id"][:12] + "...",
            )
        else:
            logger.warning("No active market found at startup -- will retry on next transition")

        # 5. Initialise strategies
        momentum_config = self.config.get("momentum", {})
        spread_config = self.config.get("spread_capture", {})
        paper_mode = not self.live

        self._momentum = MomentumStrategy(
            poly_client=self._trading_client,
            trade_logger=self._trade_logger,
            config=momentum_config,
            paper_mode=paper_mode,
        )

        self._spread_capture = SpreadCaptureStrategy(
            poly_client=self._trading_client,
            trade_logger=self._trade_logger,
            config=spread_config,
            paper_mode=paper_mode,
        )

        # 6. Start Binance WebSocket feed with momentum callback
        min_delta = momentum_config.get("entry_min_delta", 0.003)
        lookback = momentum_config.get("lookback_secs", 3)

        self._binance_feed = BinanceFeed(
            symbol="btcusdt",
            on_signal=self._momentum.on_signal,
            min_delta=min_delta,
            lookback_secs=lookback,
        )

        self._binance_task = asyncio.create_task(
            self._binance_feed.start(), name="binance_feed"
        )

        # 7. Start polling loop (spread capture + resolutions)
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="poll_loop"
        )

        # 8. Schedule market transition
        self._transition_task = asyncio.create_task(
            self._transition_loop(), name="transition_loop"
        )

        logger.info("Gabagool bot is running. Press Ctrl+C to stop.")

        # Wait for all tasks; if any exits unexpectedly, shut down cleanly
        done, pending = await asyncio.wait(
            [self._binance_task, self._poll_task, self._transition_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # If we get here and we're still supposed to be running, something crashed
        for task in done:
            if task.exception() and self._running:
                logger.error(
                    "Task {} exited with error: {}",
                    task.get_name(),
                    task.exception(),
                )
                await self.shutdown()

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """
        Poll Polymarket orderbook for spread capture opportunities
        and check trade resolutions.

        Runs every poll_interval_ms (default 500ms).
        """
        poll_interval = self.config.get("general", {}).get("poll_interval_ms", 500) / 1000.0
        spread_enabled = self.config.get("spread_capture", {}).get("enabled", True)

        while self._running:
            try:
                # Get current best prices from Polymarket
                prices = await asyncio.to_thread(
                    self._trading_client.get_best_prices
                )

                # Spread capture check
                if spread_enabled and self._spread_capture is not None:
                    self._spread_capture.execute(prices)

                # Resolution check -- use YES ask as proxy for current YES price
                yes_price = prices.get("yes_ask")
                if self._trade_logger is not None:
                    self._trade_logger.check_resolutions(current_yes_price=yes_price)

            except Exception as exc:
                logger.error("Poll loop error: {}", exc)

            await asyncio.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Market transition loop
    # ------------------------------------------------------------------

    async def _transition_loop(self) -> None:
        """
        Sleep until the next 15-minute boundary, then perform a market
        transition. Repeats indefinitely while the bot is running.
        """
        while self._running:
            interval = self._config.get("general", {}).get("market_interval_secs", 300)
            wait_secs = _seconds_until_next_boundary(interval)
            logger.info(
                "Next market transition in {:.0f}s ({:.1f} min)",
                wait_secs,
                wait_secs / 60,
            )

            # Sleep in small increments so we can exit promptly on shutdown
            slept = 0.0
            while slept < wait_secs and self._running:
                chunk = min(5.0, wait_secs - slept)
                await asyncio.sleep(chunk)
                slept += chunk

            if not self._running:
                break

            await self._market_transition()

    async def _market_transition(self) -> None:
        """
        Handle a 15-minute market boundary.

        Steps:
          1. Reset strategy window counters
          2. Invalidate cached market so discovery fetches fresh data
          3. Discover the new market
          4. Log transition
        """
        logger.info("--- MARKET TRANSITION ---")

        # Reset strategies for the new window
        if self._momentum is not None:
            self._momentum.reset_window()
        if self._spread_capture is not None:
            self._spread_capture.reset_window()

        # Force re-discovery by clearing the cached market slug on the real client
        if self._poly_client is not None:
            self._poly_client._market_slug = None
            self._poly_client._market = None

        # Discover the new market
        try:
            market = await asyncio.to_thread(
                self._trading_client.discover_current_market
            )
            if market:
                logger.info(
                    "New market discovered: {} | YES={} | NO={}",
                    market["slug"],
                    market["yes_token_id"][:12] + "...",
                    market["no_token_id"][:12] + "...",
                )
            else:
                logger.warning("No market found after transition -- will retry next poll")
        except Exception as exc:
            logger.error("Market discovery failed during transition: {}", exc)

        logger.info("--- TRANSITION COMPLETE ---")

    # ------------------------------------------------------------------
    # Banner + summary
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        """Print startup info banner."""
        mode = "LIVE" if self.live else "PAPER"
        momentum_cfg = self.config.get("momentum", {})
        spread_cfg = self.config.get("spread_capture", {})
        general_cfg = self.config.get("general", {})

        banner = f"""
================================================================================
   GABAGOOL BOT -- Polymarket BTC 15-Minute Trading Bot
================================================================================
   Mode:           {mode}
   Budget:         ${self.budget:.2f}
   Poll interval:  {general_cfg.get('poll_interval_ms', 500)}ms
   Market window:  {general_cfg.get('market_interval_secs', 300)}s ({general_cfg.get('market_interval_secs', 300) // 60} min)
   ----
   Momentum:       {'ON' if momentum_cfg.get('enabled', True) else 'OFF'}
     min_delta:    {momentum_cfg.get('entry_min_delta', 0.003)}
     cooldown:     {momentum_cfg.get('cooldown_secs', 30)}s
     order_size:   ${momentum_cfg.get('order_size_usd', 5.0):.2f}
     max/window:   {momentum_cfg.get('max_trades_per_window', 5)}
   ----
   Spread Capture: {'ON' if spread_cfg.get('enabled', True) else 'OFF'}
     threshold:    {spread_cfg.get('spread_threshold', 0.96)}
     order_size:   ${spread_cfg.get('order_size_usd', 5.0):.2f}
     cooldown:     {spread_cfg.get('cooldown_secs', 10)}s
   ----
   Started at:     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
================================================================================
"""
        # Use print for banner so it always appears regardless of log level
        print(banner)
        logger.info("Gabagool bot starting in {} mode", mode)

    def _print_summary(self) -> None:
        """Print session summary on exit."""
        elapsed = time.time() - self._start_time if self._start_time else 0
        minutes = elapsed / 60.0

        summary_lines = [
            "",
            "=" * 72,
            "   GABAGOOL SESSION SUMMARY",
            "=" * 72,
            f"   Duration:       {minutes:.1f} minutes",
            f"   Mode:           {'LIVE' if self.live else 'PAPER'}",
        ]

        # Trade logger summary
        if self._trade_logger is not None:
            stats = self._trade_logger.get_summary()
            summary_lines.extend([
                f"   Total trades:   {stats['total_trades']}",
                f"   Resolved:       {stats['resolved']} (W:{stats['wins']} L:{stats['losses']})",
                f"   Pending:        {stats['pending']}",
                f"   Win rate:       {stats['win_rate']:.1%}",
                f"   Net P&L:        ${stats['net_pnl']:.4f}",
                f"   ROI:            {stats['roi_pct']:.2f}%",
            ])

            # Per-strategy breakdown
            for strat_name, strat_data in stats.get("by_strategy", {}).items():
                summary_lines.append(
                    f"   [{strat_name}] trades={strat_data['total']} "
                    f"W={strat_data['wins']} L={strat_data['losses']} "
                    f"pnl=${strat_data['net_pnl']:.4f}"
                )

        # Strategy stats
        if self._momentum is not None:
            m_stats = self._momentum.get_stats()
            summary_lines.append(
                f"   [momentum] signals={m_stats['total_signals']} "
                f"trades={m_stats['total_trades']} "
                f"skipped_cd={m_stats['skipped']['cooldown']} "
                f"skipped_wl={m_stats['skipped']['window_limit']}"
            )

        if self._spread_capture is not None:
            s_stats = self._spread_capture.get_stats()
            summary_lines.append(
                f"   [spread] trades={s_stats['trade_count']} "
                f"opportunities={s_stats['opportunities_seen']} "
                f"pair_cost={s_stats['pair_cost']:.4f} "
                f"g_profit=${s_stats['guaranteed_profit']:.4f}"
            )

        # Paper engine balance
        if self._paper_engine is not None:
            portfolio = self._paper_engine.get_portfolio()
            summary_lines.append(
                f"   [paper] balance=${portfolio['balance']:.2f} "
                f"total_value=${portfolio['total_value']:.2f} "
                f"fills={portfolio['fill_count']}"
            )

        summary_lines.append("=" * 72)
        summary_lines.append("")

        print("\n".join(summary_lines))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Clean shutdown of all components."""
        if not self._running:
            return
        self._running = False
        logger.info("Shutting down Gabagool bot...")

        # Stop Binance feed
        if self._binance_feed is not None:
            await self._binance_feed.stop()

        # Cancel async tasks
        for task in [self._binance_task, self._poll_task, self._transition_task]:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Close Polymarket client
        if self._poly_client is not None:
            self._poly_client.close()

        # Print summary
        self._print_summary()

        logger.info("Gabagool bot stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gabagool -- Hybrid Polymarket BTC 15-Minute Trading Bot"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (default: paper mode)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.json (default: gabagool/config.json)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Override budget in USD",
    )
    args = parser.parse_args()

    # Load .env from project root (parent of gabagool/)
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    # Load config
    config_path = args.config or str(Path(__file__).resolve().parent / "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.error("Config file not found: {}", config_path)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in config file {}: {}", config_path, exc)
        sys.exit(1)

    # Apply budget override
    if args.budget is not None:
        config.setdefault("general", {})["budget_usd"] = args.budget

    budget = config.get("general", {}).get("budget_usd", 60.0)

    bot = GabagoolBot(config=config, live=args.live, budget=budget)

    # Set up event loop with signal handlers
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.shutdown()))

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.shutdown())
    finally:
        # Give pending tasks a moment to clean up
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.close()


if __name__ == "__main__":
    main()
