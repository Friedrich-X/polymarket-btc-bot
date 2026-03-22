"""
Polymarket CLOB Client Wrapper for Gabagool Bot

Wraps py-clob-client for market discovery, order book reading, and order placement.
All py-clob-client calls are synchronous; use asyncio.to_thread() for async contexts.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from loguru import logger

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)
from py_clob_client.constants import POLYGON

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
MARKET_INTERVAL_SECS = 900  # 15 minutes


def _current_market_slug() -> str:
    """Build the slug for the current BTC 15-min market window."""
    now_unix = int(time.time())
    window_ts = (now_unix // MARKET_INTERVAL_SECS) * MARKET_INTERVAL_SECS
    return f"btc-updown-15m-{window_ts}"


class GabagoolPolyClient:
    """Polymarket CLOB client tailored for the Gabagool BTC 15-min bot."""

    def __init__(self, live: bool = False) -> None:
        """
        Initialise the client.

        Args:
            live: When *False* the client is created in an inert state and will
                  not attempt any network calls.  Call :meth:`connect` to
                  activate it.
        """
        self.live = live
        self._client: ClobClient | None = None
        self._http: httpx.Client | None = None

        # Cached market data for the current 15-min window
        self._market: dict[str, Any] | None = None
        self._market_slug: str | None = None

        if live:
            self.connect()

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Set up py-clob-client with API credentials from the environment."""
        load_dotenv()

        pk = os.environ.get("POLYMARKET_PK", "")
        api_key = os.environ.get("POLYMARKET_API_KEY", "")
        api_secret = os.environ.get("POLYMARKET_API_SECRET", "")
        passphrase = os.environ.get("POLYMARKET_PASSPHRASE", "")
        funder = os.environ.get("POLYMARKET_FUNDER") or None

        if not all([pk, api_key, api_secret, passphrase]):
            raise RuntimeError(
                "Missing Polymarket credentials. "
                "Ensure POLYMARKET_PK, POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
                "and POLYMARKET_PASSPHRASE are set in the environment / .env file."
            )

        # Strip the 0x prefix that wallets typically include
        if pk.startswith("0x"):
            pk = pk[2:]

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        )

        self._client = ClobClient(
            host=CLOB_HOST,
            chain_id=POLYGON,
            key=pk,
            creds=creds,
            funder=funder,
        )

        # Reusable HTTP client for Gamma API requests
        self._http = httpx.Client(timeout=15.0)

        logger.info("GabagoolPolyClient connected to Polymarket CLOB")

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    def discover_current_market(self) -> dict[str, Any] | None:
        """
        Find the current BTC 15-min market via the Gamma API.

        Returns a dict with keys:
            condition_id, yes_token_id, no_token_id, slug, end_time

        Returns ``None`` when no matching market is found.
        """
        slug = _current_market_slug()

        # Return cached result if the window hasn't changed
        if self._market is not None and self._market_slug == slug:
            return self._market

        url = f"{GAMMA_API_BASE}/markets"
        params = {"slug": slug}

        try:
            resp = self._gamma_get(url, params)
        except Exception:
            logger.exception("Gamma API request failed for slug={}", slug)
            return None

        if not resp:
            logger.warning("No market found for slug={}", slug)
            return None

        # Gamma returns a list — take the first match
        market_data = resp[0] if isinstance(resp, list) else resp

        # Extract token IDs.  Gamma API returns:
        #   outcomes:      JSON string like '["Up","Down"]'
        #   clobTokenIds:  JSON string like '["<id1>","<id2>"]'
        # Outcome order matches token ID order: Up=index 0, Down=index 1.
        raw_outcomes = market_data.get("outcomes", "[]")
        raw_token_ids = market_data.get("clobTokenIds", "[]")

        # Values may already be Python lists or JSON-encoded strings
        outcomes = (
            json.loads(raw_outcomes)
            if isinstance(raw_outcomes, str)
            else (raw_outcomes or [])
        )
        token_ids = (
            json.loads(raw_token_ids)
            if isinstance(raw_token_ids, str)
            else (raw_token_ids or [])
        )

        if len(outcomes) != 2 or len(token_ids) != 2:
            logger.error(
                "Unexpected outcomes/tokenIds lengths for slug={} "
                "(outcomes={}, tokenIds={})",
                slug,
                outcomes,
                token_ids,
            )
            return None

        # Map outcomes to YES/NO semantics.
        # "Up" → YES (price went up), "Down" → NO (price went down).
        outcome_to_idx = {o.upper(): i for i, o in enumerate(outcomes)}
        yes_idx = outcome_to_idx.get("UP")
        no_idx = outcome_to_idx.get("DOWN")

        if yes_idx is None or no_idx is None:
            logger.error(
                "Could not map outcomes to UP/DOWN for slug={} (outcomes={})",
                slug,
                outcomes,
            )
            return None

        yes_token = token_ids[yes_idx]
        no_token = token_ids[no_idx]

        self._market = {
            "condition_id": market_data.get("conditionId", ""),
            "yes_token_id": yes_token,
            "no_token_id": no_token,
            "slug": slug,
            "end_time": market_data.get("endDate", ""),
        }
        self._market_slug = slug

        logger.info(
            "Discovered market slug={} | YES={} | NO={}",
            slug,
            yes_token[:12] + "...",
            no_token[:12] + "...",
        )
        return self._market

    # ------------------------------------------------------------------
    # Order book
    # ------------------------------------------------------------------

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """
        Fetch the order book for a single token.

        Returns::

            {
                "bids": [(price, size), ...],
                "asks": [(price, size), ...],
            }
        """
        self._require_client()
        try:
            book = self._client.get_order_book(token_id)  # type: ignore[union-attr]
            bids = [
                (float(entry.price), float(entry.size)) for entry in (book.bids or [])
            ]
            asks = [
                (float(entry.price), float(entry.size)) for entry in (book.asks or [])
            ]
            return {"bids": bids, "asks": asks}
        except Exception:
            logger.exception("Failed to fetch order book for token_id={}", token_id)
            return {"bids": [], "asks": []}

    def get_best_prices(self) -> dict[str, float | None]:
        """
        Get best bid/ask for both YES and NO tokens of the current market.

        Requires :meth:`discover_current_market` to have been called first.

        Returns::

            {"yes_ask": ..., "yes_bid": ..., "no_ask": ..., "no_bid": ...}
        """
        if self._market is None:
            logger.error("No market discovered — call discover_current_market() first")
            return {"yes_ask": None, "yes_bid": None, "no_ask": None, "no_bid": None}

        yes_book = self.get_order_book(self._market["yes_token_id"])
        no_book = self.get_order_book(self._market["no_token_id"])

        def _best_bid(book: dict) -> float | None:
            bids = book.get("bids", [])
            return max((p for p, _ in bids), default=None) if bids else None

        def _best_ask(book: dict) -> float | None:
            asks = book.get("asks", [])
            return min((p for p, _ in asks), default=None) if asks else None

        return {
            "yes_ask": _best_ask(yes_book),
            "yes_bid": _best_bid(yes_book),
            "no_ask": _best_ask(no_book),
            "no_bid": _best_bid(no_book),
        }

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return the current USDC (collateral) balance."""
        self._require_client()
        try:
            result = self._client.get_balance_allowance(  # type: ignore[union-attr]
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            # result is a dict with "balance" key (string of wei or float)
            balance_raw = result.get("balance", "0") if isinstance(result, dict) else 0
            return float(balance_raw) / 1e6  # USDC has 6 decimals
        except Exception:
            logger.exception("Failed to fetch USDC balance")
            return 0.0

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_market_buy(
        self, token_id: str, amount_usd: float
    ) -> dict[str, Any] | None:
        """
        Place a Fill-or-Kill (FOK) market buy order.

        Args:
            token_id: The token to buy (YES or NO token ID).
            amount_usd: USD amount to spend.

        Returns:
            The API response dict on success, or ``None`` on failure.
        """
        self._require_client()

        if amount_usd <= 0:
            logger.error("amount_usd must be positive, got {}", amount_usd)
            return None

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usd,
                side="BUY",
            )
            # create_market_order signs the order; post_order submits it
            signed_order = self._client.create_market_order(order_args)  # type: ignore[union-attr]
            resp = self._client.post_order(signed_order, orderType=OrderType.FOK)  # type: ignore[union-attr]
            logger.info(
                "Market BUY placed: token={} amount_usd={} resp={}",
                token_id[:12] + "...",
                amount_usd,
                resp,
            )
            return resp
        except Exception:
            logger.exception(
                "Market BUY failed: token={} amount_usd={}",
                token_id[:12] + "...",
                amount_usd,
            )
            return None

    def place_limit_buy(
        self, token_id: str, price: float, size_tokens: float
    ) -> dict[str, Any] | None:
        """
        Place a Good-Til-Cancelled (GTC) limit buy order.

        Args:
            token_id: The token to buy.
            price: Limit price (0 < price < 1).
            size_tokens: Number of tokens to buy (minimum 5).

        Returns:
            The API response dict on success, or ``None`` on failure.
        """
        self._require_client()

        if size_tokens < 5:
            logger.error(
                "Limit order size must be >= 5 tokens, got {}", size_tokens
            )
            return None

        if not (0 < price < 1):
            logger.error("Limit price must be between 0 and 1, got {}", price)
            return None

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size_tokens,
                side="BUY",
            )
            signed_order = self._client.create_order(order_args)  # type: ignore[union-attr]
            resp = self._client.post_order(signed_order, orderType=OrderType.GTC)  # type: ignore[union-attr]
            logger.info(
                "Limit BUY placed: token={} price={} size={} resp={}",
                token_id[:12] + "...",
                price,
                size_tokens,
                resp,
            )
            return resp
        except Exception:
            logger.exception(
                "Limit BUY failed: token={} price={} size={}",
                token_id[:12] + "...",
                price,
                size_tokens,
            )
            return None

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by its ID. Returns *True* on success."""
        self._require_client()
        try:
            self._client.cancel(order_id)  # type: ignore[union-attr]
            logger.info("Cancelled order {}", order_id)
            return True
        except Exception:
            logger.exception("Failed to cancel order {}", order_id)
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders. Returns *True* on success."""
        self._require_client()
        try:
            self._client.cancel_all()  # type: ignore[union-attr]
            logger.info("Cancelled all open orders")
            return True
        except Exception:
            logger.exception("Failed to cancel all orders")
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_client(self) -> None:
        """Raise if the CLOB client hasn't been initialised."""
        if self._client is None:
            raise RuntimeError(
                "ClobClient not initialised. Call connect() or pass live=True."
            )

    def _gamma_get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> Any:
        """
        Perform a GET request against the Gamma API.

        Creates a one-shot httpx client if ``self._http`` is not available
        (e.g. when called before :meth:`connect`).
        """
        if self._http is not None:
            resp = self._http.get(url, params=params)
        else:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, params=params)

        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release resources (HTTP connection pool)."""
        if self._http is not None:
            self._http.close()
            self._http = None
        logger.debug("GabagoolPolyClient closed")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
