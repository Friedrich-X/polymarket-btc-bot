"""
Spread Capture (Arbitrage) Strategy for Gabagool Bot

Monitors YES and NO token prices on Polymarket BTC 15-minute markets.
When the combined ask price (YES_ask + NO_ask) falls below a threshold,
buys both sides to lock in risk-free profit.

Guaranteed payout: min(qty_yes, qty_no) * $1.00 at resolution, regardless
of outcome.  Profit = payout - total_cost.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from loguru import logger


class SpreadCaptureStrategy:
    """Pair-cost arbitrage on Polymarket binary outcome markets."""

    def __init__(
        self,
        poly_client: Any,
        trade_logger: Any,
        config: dict,
        paper_mode: bool = True,
    ) -> None:
        """
        Args:
            poly_client: GabagoolPolyClient instance (must already have a
                         discovered market via discover_current_market()).
            trade_logger: TradeLogger instance for recording trades.
            config: Strategy configuration dict with keys:
                - spread_threshold (float): Max combined ask to trigger (default 0.96).
                - order_size_usd (float): USD per leg per buy (default 5.0).
                - max_imbalance_ratio (float): Max ratio of larger/smaller side (default 1.5).
                - cooldown_secs (int): Seconds between buy attempts (default 10).
            paper_mode: When True, simulate fills at the ask price.
        """
        self.poly_client = poly_client
        self.trade_logger = trade_logger
        self.paper_mode = paper_mode

        # Config with defaults
        self.spread_threshold: float = config.get("spread_threshold", 0.96)
        self.order_size_usd: float = config.get("order_size_usd", 5.0)
        self.max_imbalance_ratio: float = config.get("max_imbalance_ratio", 1.5)
        self.cooldown_secs: int = config.get("cooldown_secs", 10)

        # Cumulative position tracking
        self.qty_yes: float = 0.0
        self.qty_no: float = 0.0
        self.cost_yes: float = 0.0
        self.cost_no: float = 0.0

        # Cooldown
        self._last_buy_ts: float = 0.0

        # Trade count for stats
        self._trade_count: int = 0
        self._opportunities_seen: int = 0

        logger.info(
            "SpreadCaptureStrategy initialised | threshold={} size_usd={} "
            "imbalance_limit={} cooldown={}s paper={}",
            self.spread_threshold,
            self.order_size_usd,
            self.max_imbalance_ratio,
            self.cooldown_secs,
            self.paper_mode,
        )

    # ------------------------------------------------------------------
    # Opportunity detection
    # ------------------------------------------------------------------

    def check_opportunity(self, prices: dict) -> dict | None:
        """
        Check if a spread capture opportunity exists.

        Args:
            prices: Dict with keys yes_ask, yes_bid, no_ask, no_bid.
                    Values may be float or None.

        Returns:
            Opportunity dict with spread details, or None if no opportunity.
        """
        yes_ask = prices.get("yes_ask")
        no_ask = prices.get("no_ask")

        if yes_ask is None or no_ask is None:
            return None

        combined_ask = yes_ask + no_ask

        if combined_ask >= self.spread_threshold:
            return None

        spread = 1.0 - combined_ask  # Guaranteed profit per token pair

        self._opportunities_seen += 1

        opportunity = {
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "combined_ask": round(combined_ask, 6),
            "spread": round(spread, 6),
            "profit_per_pair_usd": round(spread, 6),
        }

        logger.info(
            "Spread opportunity detected | YES_ask={:.4f} NO_ask={:.4f} "
            "combined={:.4f} spread={:.4f}",
            yes_ask,
            no_ask,
            combined_ask,
            spread,
        )

        return opportunity

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, prices: dict) -> dict | None:
        """
        Execute a spread capture if an opportunity exists and conditions are met.

        Args:
            prices: Dict with keys yes_ask, yes_bid, no_ask, no_bid.

        Returns:
            Result dict on execution, or None if no action taken.
        """
        # Check opportunity
        opportunity = self.check_opportunity(prices)
        if opportunity is None:
            return None

        # Profit already locked — stop buying
        if self._is_profit_locked():
            logger.info(
                "Profit already locked (pair_cost={:.4f}) — skipping",
                self.get_pair_cost(),
            )
            return None

        # Cooldown check
        now = time.time()
        if now - self._last_buy_ts < self.cooldown_secs:
            remaining = self.cooldown_secs - (now - self._last_buy_ts)
            logger.debug("Cooldown active — {:.1f}s remaining", remaining)
            return None

        # Imbalance check: would buying both sides keep us balanced?
        if not self._can_buy_balanced():
            logger.warning(
                "Position imbalanced (YES={:.2f} NO={:.2f} ratio={:.2f}) — skipping",
                self.qty_yes,
                self.qty_no,
                self._imbalance_ratio(),
            )
            return None

        yes_ask = opportunity["yes_ask"]
        no_ask = opportunity["no_ask"]

        # Calculate balanced token quantities from USD budget
        # Buy equal number of tokens on each side
        qty_yes_tokens = self.order_size_usd / yes_ask
        qty_no_tokens = self.order_size_usd / no_ask
        # Use the smaller quantity so both sides are equal
        balanced_qty = min(qty_yes_tokens, qty_no_tokens)

        # Enforce minimum order size (Polymarket requires >= 5 tokens)
        if balanced_qty < 5.0:
            logger.warning(
                "Balanced quantity {:.2f} below minimum 5 tokens — skipping",
                balanced_qty,
            )
            return None

        # Execute both legs
        yes_result = self._buy_side("YES", yes_ask, balanced_qty)
        no_result = self._buy_side("NO", no_ask, balanced_qty)

        self._last_buy_ts = time.time()

        # Determine fill status
        yes_filled = yes_result is not None
        no_filled = no_result is not None

        if yes_filled and no_filled:
            fill_status = "both_filled"
        elif yes_filled or no_filled:
            fill_status = "partial_fill"
            logger.warning(
                "Partial fill — YES={} NO={}",
                "filled" if yes_filled else "missed",
                "filled" if no_filled else "missed",
            )
        else:
            fill_status = "no_fill"
            logger.error("Both legs failed to fill")
            return None

        # Log the spread pair via TradeLogger
        market = self.poly_client._market
        market_slug = market["slug"] if market else "unknown"

        if yes_filled and no_filled:
            # Record as a paired spread trade
            self.trade_logger.record_spread_pair(
                yes_price=yes_ask,
                no_price=no_ask,
                quantity=balanced_qty,
                market_slug=market_slug,
                is_paper=self.paper_mode,
            )
        else:
            # Record individual legs that filled
            if yes_filled:
                self.trade_logger.record_trade(
                    strategy="spread_capture",
                    token_side="YES",
                    price=yes_ask,
                    quantity=balanced_qty,
                    cost_usd=yes_ask * balanced_qty,
                    market_slug=market_slug,
                    is_paper=self.paper_mode,
                )
            if no_filled:
                self.trade_logger.record_trade(
                    strategy="spread_capture",
                    token_side="NO",
                    price=no_ask,
                    quantity=balanced_qty,
                    cost_usd=no_ask * balanced_qty,
                    market_slug=market_slug,
                    is_paper=self.paper_mode,
                )

        self._trade_count += 1

        result = {
            "action": "spread_capture",
            "fill_status": fill_status,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "quantity": round(balanced_qty, 4),
            "pair_cost": round(self.get_pair_cost(), 6),
            "guaranteed_profit": round(self.get_guaranteed_profit(), 6),
            "profit_locked": self._is_profit_locked(),
        }

        logger.info(
            "Spread capture executed | qty={:.2f} pair_cost={:.4f} "
            "g_profit=${:.4f} locked={}",
            balanced_qty,
            self.get_pair_cost(),
            self.get_guaranteed_profit(),
            self._is_profit_locked(),
        )

        return result

    # ------------------------------------------------------------------
    # Side execution
    # ------------------------------------------------------------------

    def _buy_side(self, side: str, price: float, quantity: float) -> dict | None:
        """
        Buy YES or NO tokens.

        Args:
            side: "YES" or "NO".
            price: Limit price (the current ask).
            quantity: Number of tokens to buy.

        Returns:
            API response dict on success, or None on failure.
        """
        market = self.poly_client._market
        if market is None:
            logger.error("No market discovered — cannot place order")
            return None

        token_id = market["yes_token_id"] if side == "YES" else market["no_token_id"]
        cost = price * quantity

        if self.paper_mode:
            # Simulate fill at ask price
            result = {
                "orderID": f"paper-{uuid.uuid4().hex[:8]}",
                "status": "MATCHED",
                "side": side,
                "price": price,
                "size": quantity,
            }
            logger.debug(
                "[PAPER] {} buy filled | price={:.4f} qty={:.2f} cost=${:.4f}",
                side,
                price,
                quantity,
                cost,
            )
        else:
            # Place GTC limit order at the ask price to act as taker
            result = self.poly_client.place_limit_buy(
                token_id=token_id,
                price=price,
                size_tokens=quantity,
            )
            if result is None:
                logger.error("{} leg failed to place", side)
                return None

        # Update cumulative position
        if side == "YES":
            self.qty_yes += quantity
            self.cost_yes += cost
        else:
            self.qty_no += quantity
            self.cost_no += cost

        return result

    # ------------------------------------------------------------------
    # Position analysis
    # ------------------------------------------------------------------

    def _is_balanced(self) -> bool:
        """Check if YES and NO positions are reasonably balanced."""
        return self._imbalance_ratio() <= self.max_imbalance_ratio

    def _can_buy_balanced(self) -> bool:
        """
        Check if we can buy both sides without exceeding imbalance limits.

        If no position yet, always allow. Otherwise check current balance.
        """
        if self.qty_yes == 0.0 and self.qty_no == 0.0:
            return True
        return self._is_balanced()

    def _imbalance_ratio(self) -> float:
        """Ratio of larger position to smaller position."""
        if self.qty_yes == 0.0 and self.qty_no == 0.0:
            return 1.0
        if self.qty_yes == 0.0 or self.qty_no == 0.0:
            # One side is zero, the other is not — infinite imbalance,
            # but return a high finite number for comparisons.
            return float("inf")
        return max(self.qty_yes, self.qty_no) / min(self.qty_yes, self.qty_no)

    def _is_profit_locked(self) -> bool:
        """
        Check if pair cost < $1.00, meaning profit is guaranteed
        regardless of market outcome.

        Requires positions on both sides.
        """
        if self.qty_yes == 0.0 or self.qty_no == 0.0:
            return False
        return self.get_pair_cost() < 1.0

    def get_pair_cost(self) -> float:
        """
        Current average pair cost: avg_price_yes + avg_price_no.

        Returns 0.0 if no position on either side.
        """
        if self.qty_yes == 0.0 or self.qty_no == 0.0:
            return 0.0
        avg_yes = self.cost_yes / self.qty_yes
        avg_no = self.cost_no / self.qty_no
        return avg_yes + avg_no

    def get_guaranteed_profit(self) -> float:
        """
        Profit locked in so far.

        guaranteed_profit = min(qty_yes, qty_no) * $1.00 - (cost for matched qty)

        Only the matched (hedged) portion generates guaranteed profit.
        """
        matched_qty = min(self.qty_yes, self.qty_no)
        if matched_qty == 0.0:
            return 0.0

        # Cost attributable to the matched portion
        avg_yes = self.cost_yes / self.qty_yes if self.qty_yes > 0 else 0.0
        avg_no = self.cost_no / self.qty_no if self.qty_no > 0 else 0.0
        matched_cost = matched_qty * (avg_yes + avg_no)

        # Payout is $1.00 per matched pair
        payout = matched_qty * 1.0
        return payout - matched_cost

    def get_unhedged_risk(self) -> float:
        """
        Risk from unbalanced positions.

        risk = abs(qty_yes - qty_no) * max(avg_price_yes, avg_price_no)
        """
        imbalance = abs(self.qty_yes - self.qty_no)
        if imbalance == 0.0:
            return 0.0

        avg_yes = self.cost_yes / self.qty_yes if self.qty_yes > 0 else 0.0
        avg_no = self.cost_no / self.qty_no if self.qty_no > 0 else 0.0
        return imbalance * max(avg_yes, avg_no)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset_window(self) -> None:
        """Reset all position tracking for a new 15-minute market window."""
        logger.info(
            "SpreadCapture reset | trades={} pair_cost={:.4f} g_profit=${:.4f}",
            self._trade_count,
            self.get_pair_cost(),
            self.get_guaranteed_profit(),
        )
        self.qty_yes = 0.0
        self.qty_no = 0.0
        self.cost_yes = 0.0
        self.cost_no = 0.0
        self._last_buy_ts = 0.0
        self._trade_count = 0
        self._opportunities_seen = 0

    def get_stats(self) -> dict:
        """Return current strategy statistics."""
        return {
            "strategy": "spread_capture",
            "qty_yes": round(self.qty_yes, 4),
            "qty_no": round(self.qty_no, 4),
            "cost_yes": round(self.cost_yes, 4),
            "cost_no": round(self.cost_no, 4),
            "pair_cost": round(self.get_pair_cost(), 6),
            "guaranteed_profit": round(self.get_guaranteed_profit(), 6),
            "unhedged_risk": round(self.get_unhedged_risk(), 6),
            "profit_locked": self._is_profit_locked(),
            "imbalance_ratio": round(self._imbalance_ratio(), 4),
            "trade_count": self._trade_count,
            "opportunities_seen": self._opportunities_seen,
            "paper_mode": self.paper_mode,
        }
