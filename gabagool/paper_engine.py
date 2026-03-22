"""
Paper Trading Engine for Gabagool Bot

Simulates order fills using real orderbook prices from Polymarket.
Wraps GabagoolPolyClient to intercept order placement while delegating
all read-only operations (market discovery, order books) to the real client.

Never places real orders. All fills are simulated and logged via TradeLogger
with is_paper=True.
"""

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger


class PaperEngine:
    """
    Drop-in replacement for GabagoolPolyClient that simulates order fills.

    Read-only methods (discover_current_market, get_order_book, get_best_prices)
    delegate to the real poly_client for live market data. Order placement methods
    simulate fills using real orderbook ask prices and track balance/positions
    internally.
    """

    def __init__(
        self,
        poly_client: Any,
        trade_logger: Any,
        initial_balance: float = 60.0,
    ) -> None:
        """
        Args:
            poly_client: A connected GabagoolPolyClient instance used for
                         real price data (order books, market discovery).
            trade_logger: TradeLogger instance for recording paper trades.
            initial_balance: Starting USD balance for paper trading.
        """
        self.real_client = poly_client
        self.trade_logger = trade_logger
        self.balance = initial_balance
        self.positions: dict[str, dict[str, float]] = {}
        # positions maps token_id -> {"qty": float, "avg_price": float, "cost": float}

        self._order_counter = 0
        self._fills: list[dict[str, Any]] = []  # history of simulated fills

        logger.info(
            "PaperEngine initialised | balance=${:.2f} | delegating reads to real client",
            initial_balance,
        )

    # ------------------------------------------------------------------
    # Read-only methods — delegated to real client
    # ------------------------------------------------------------------

    def discover_current_market(self) -> dict[str, Any] | None:
        """Delegate to real client for live market discovery."""
        return self.real_client.discover_current_market()

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Delegate to real client for live order book data."""
        return self.real_client.get_order_book(token_id)

    def get_best_prices(self) -> dict[str, float | None]:
        """Delegate to real client for live best bid/ask prices."""
        return self.real_client.get_best_prices()

    def get_balance(self) -> float:
        """Return the simulated paper balance."""
        return self.balance

    # ------------------------------------------------------------------
    # Simulated order placement
    # ------------------------------------------------------------------

    def place_market_buy(
        self, token_id: str, amount_usd: float
    ) -> dict[str, Any] | None:
        """
        Simulate a market buy using the real ask price from the orderbook.

        Fetches the current best ask, calculates token quantity as
        amount_usd / ask_price, deducts from balance, and records the fill.

        Args:
            token_id: The token to buy (YES or NO token ID).
            amount_usd: USD amount to spend.

        Returns:
            A simulated order response dict on success, or None on failure.
        """
        if amount_usd <= 0:
            logger.error("[PAPER] amount_usd must be positive, got {}", amount_usd)
            return None

        if amount_usd > self.balance:
            logger.warning(
                "[PAPER] Insufficient balance: need ${:.2f}, have ${:.2f}",
                amount_usd,
                self.balance,
            )
            return None

        # Fetch real orderbook to get the ask price
        book = self.real_client.get_order_book(token_id)
        asks = book.get("asks", [])

        if not asks:
            logger.warning(
                "[PAPER] No asks in orderbook for token={}, cannot simulate fill",
                token_id[:12] + "...",
            )
            return None

        # Best ask is the lowest ask price
        best_ask_price = min(p for p, _ in asks)

        if best_ask_price <= 0:
            logger.error(
                "[PAPER] Invalid ask price {} for token={}",
                best_ask_price,
                token_id[:12] + "...",
            )
            return None

        # Calculate fill
        tokens_received = amount_usd / best_ask_price
        fill_cost = amount_usd

        # Deduct from balance
        self.balance -= fill_cost

        # Update position
        self._add_to_position(token_id, tokens_received, best_ask_price, fill_cost)

        # Generate a paper order ID
        self._order_counter += 1
        order_id = f"paper-{self._order_counter}-{uuid.uuid4().hex[:8]}"

        # Determine token side for logging
        token_side = self._resolve_token_side(token_id)
        market_slug = self._resolve_market_slug()

        # Log via TradeLogger
        trade_id = self.trade_logger.record_trade(
            strategy="paper_market_buy",
            token_side=token_side,
            price=best_ask_price,
            quantity=tokens_received,
            cost_usd=fill_cost,
            market_slug=market_slug,
            is_paper=True,
        )

        fill_record = {
            "order_id": order_id,
            "trade_id": trade_id,
            "type": "market_buy",
            "token_id": token_id,
            "fill_price": best_ask_price,
            "tokens": tokens_received,
            "cost_usd": fill_cost,
            "token_side": token_side,
        }
        self._fills.append(fill_record)

        logger.info(
            "[PAPER] Market BUY filled | token={} | price={:.4f} | qty={:.2f} | "
            "cost=${:.2f} | balance=${:.2f}",
            token_id[:12] + "...",
            best_ask_price,
            tokens_received,
            fill_cost,
            self.balance,
        )

        return fill_record

    def place_limit_buy(
        self, token_id: str, price: float, size_tokens: float
    ) -> dict[str, Any] | None:
        """
        Simulate a limit buy order. Fills immediately at the specified price.

        Args:
            token_id: The token to buy.
            price: Limit price (0 < price < 1).
            size_tokens: Number of tokens to buy.

        Returns:
            A simulated order response dict on success, or None on failure.
        """
        if not (0 < price < 1):
            logger.error("[PAPER] Limit price must be between 0 and 1, got {}", price)
            return None

        if size_tokens <= 0:
            logger.error(
                "[PAPER] size_tokens must be positive, got {}", size_tokens
            )
            return None

        fill_cost = price * size_tokens

        if fill_cost > self.balance:
            logger.warning(
                "[PAPER] Insufficient balance for limit buy: need ${:.2f}, have ${:.2f}",
                fill_cost,
                self.balance,
            )
            return None

        # Deduct from balance
        self.balance -= fill_cost

        # Update position
        self._add_to_position(token_id, size_tokens, price, fill_cost)

        # Generate paper order ID
        self._order_counter += 1
        order_id = f"paper-{self._order_counter}-{uuid.uuid4().hex[:8]}"

        # Determine token side for logging
        token_side = self._resolve_token_side(token_id)
        market_slug = self._resolve_market_slug()

        # Log via TradeLogger
        trade_id = self.trade_logger.record_trade(
            strategy="paper_limit_buy",
            token_side=token_side,
            price=price,
            quantity=size_tokens,
            cost_usd=fill_cost,
            market_slug=market_slug,
            is_paper=True,
        )

        fill_record = {
            "order_id": order_id,
            "trade_id": trade_id,
            "type": "limit_buy",
            "token_id": token_id,
            "fill_price": price,
            "tokens": size_tokens,
            "cost_usd": fill_cost,
            "token_side": token_side,
        }
        self._fills.append(fill_record)

        logger.info(
            "[PAPER] Limit BUY filled | token={} | price={:.4f} | qty={:.2f} | "
            "cost=${:.2f} | balance=${:.2f}",
            token_id[:12] + "...",
            price,
            size_tokens,
            fill_cost,
            self.balance,
        )

        return fill_record

    # ------------------------------------------------------------------
    # Cancellation (no-ops for paper trading)
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> bool:
        """Paper orders always cancel successfully."""
        logger.debug("[PAPER] Cancelled order {}", order_id)
        return True

    def cancel_all(self) -> bool:
        """Paper orders always cancel successfully."""
        logger.debug("[PAPER] Cancelled all paper orders")
        return True

    # ------------------------------------------------------------------
    # Paper-specific methods
    # ------------------------------------------------------------------

    def resolve_positions(self, yes_won: bool) -> float:
        """
        Resolve all open positions at market end.

        Winning tokens (YES if yes_won=True, NO if yes_won=False) pay $1.00
        each. Losing tokens pay $0.00.

        Args:
            yes_won: True if YES outcome won, False if NO outcome won.

        Returns:
            The total payout added to balance.
        """
        market = self.real_client.discover_current_market()
        yes_token_id = market["yes_token_id"] if market else None
        no_token_id = market["no_token_id"] if market else None

        total_payout = 0.0

        for token_id, pos in list(self.positions.items()):
            qty = pos["qty"]
            cost = pos["cost"]

            # Determine if this token is a winner
            is_yes_token = token_id == yes_token_id
            is_no_token = token_id == no_token_id

            if (is_yes_token and yes_won) or (is_no_token and not yes_won):
                # Winner: each token pays $1.00
                payout = qty * 1.0
                pnl = payout - cost
                total_payout += payout
                logger.info(
                    "[PAPER] Position resolved WIN | token={} | qty={:.2f} | "
                    "payout=${:.2f} | pnl=${:.4f}",
                    token_id[:12] + "...",
                    qty,
                    payout,
                    pnl,
                )
            else:
                # Loser: tokens worth $0
                pnl = -cost
                logger.info(
                    "[PAPER] Position resolved LOSS | token={} | qty={:.2f} | "
                    "payout=$0.00 | pnl=${:.4f}",
                    token_id[:12] + "...",
                    qty,
                    pnl,
                )

        # Add total payout to balance
        self.balance += total_payout

        logger.info(
            "[PAPER] All positions resolved | yes_won={} | payout=${:.2f} | "
            "new_balance=${:.2f}",
            yes_won,
            total_payout,
            self.balance,
        )

        # Clear all positions
        self.positions.clear()

        return total_payout

    def get_portfolio(self) -> dict[str, Any]:
        """
        Get current positions and unrealized P&L.

        Returns a dict with:
            - balance: current USD balance
            - positions: dict of token_id -> {qty, avg_price, cost, current_value, unrealized_pnl}
            - total_value: balance + sum of position current values
            - total_unrealized_pnl: sum of unrealized P&L across all positions
        """
        portfolio_positions: dict[str, Any] = {}
        total_unrealized_pnl = 0.0
        total_position_value = 0.0

        # Try to get current prices for unrealized P&L
        best_prices = self.real_client.get_best_prices()
        market = self.real_client.discover_current_market()

        yes_token_id = market["yes_token_id"] if market else None
        no_token_id = market["no_token_id"] if market else None

        for token_id, pos in self.positions.items():
            qty = pos["qty"]
            avg_price = pos["avg_price"]
            cost = pos["cost"]

            # Determine current bid price for this token
            current_bid = None
            if token_id == yes_token_id and best_prices.get("yes_bid") is not None:
                current_bid = best_prices["yes_bid"]
            elif token_id == no_token_id and best_prices.get("no_bid") is not None:
                current_bid = best_prices["no_bid"]

            if current_bid is not None:
                current_value = qty * current_bid
                unrealized_pnl = current_value - cost
            else:
                current_value = qty * avg_price  # fallback to avg price
                unrealized_pnl = 0.0

            total_position_value += current_value
            total_unrealized_pnl += unrealized_pnl

            portfolio_positions[token_id] = {
                "qty": round(qty, 6),
                "avg_price": round(avg_price, 6),
                "cost": round(cost, 4),
                "current_value": round(current_value, 4),
                "unrealized_pnl": round(unrealized_pnl, 4),
            }

        return {
            "balance": round(self.balance, 4),
            "positions": portfolio_positions,
            "total_value": round(self.balance + total_position_value, 4),
            "total_unrealized_pnl": round(total_unrealized_pnl, 4),
            "fill_count": len(self._fills),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _add_to_position(
        self,
        token_id: str,
        qty: float,
        price: float,
        cost: float,
    ) -> None:
        """Add tokens to an existing position or create a new one."""
        if token_id in self.positions:
            existing = self.positions[token_id]
            total_qty = existing["qty"] + qty
            total_cost = existing["cost"] + cost
            # Weighted average price
            avg_price = total_cost / total_qty if total_qty > 0 else 0.0
            self.positions[token_id] = {
                "qty": total_qty,
                "avg_price": avg_price,
                "cost": total_cost,
            }
        else:
            self.positions[token_id] = {
                "qty": qty,
                "avg_price": price,
                "cost": cost,
            }

    def _resolve_token_side(self, token_id: str) -> str:
        """Determine if token_id corresponds to YES or NO."""
        market = self.real_client.discover_current_market()
        if market is None:
            return "UNKNOWN"
        if token_id == market.get("yes_token_id"):
            return "YES"
        if token_id == market.get("no_token_id"):
            return "NO"
        return "UNKNOWN"

    def _resolve_market_slug(self) -> str:
        """Get the current market slug."""
        market = self.real_client.discover_current_market()
        if market is None:
            return "unknown"
        return market.get("slug", "unknown")
