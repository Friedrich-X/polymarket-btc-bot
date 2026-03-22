"""
Trade Logger for Gabagool Bot
JSON-based trade logging with resolution tracking and session statistics.
"""
import json
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


MARKET_INTERVAL_SECS = 300  # 5 minutes (default)
RESOLUTION_GRACE_SECS = 30
RESOLUTION_UNKNOWN_SECS = 120  # Mark UNKNOWN if still unclear after this
YES_WIN_THRESHOLD = 0.95
NO_WIN_THRESHOLD = 0.05


@dataclass
class Trade:
    trade_id: str
    timestamp: str  # ISO format
    strategy: str  # "momentum" or "spread_capture"
    token_side: str  # "YES" or "NO"
    price: float
    quantity: float
    cost_usd: float
    market_slug: str
    resolve_at: str  # ISO format -- when the 15-min market ends
    resolve_at_ts: float  # Unix timestamp
    outcome: str = "PENDING"  # "PENDING", "WIN", "LOSS", "UNKNOWN"
    pnl: float = 0.0  # 0 until resolved
    resolved_price: Optional[float] = None  # final YES price at resolution
    is_paper: bool = True
    spread_pair_id: Optional[str] = None  # links YES/NO legs of spread trades


def _trade_to_dict(trade: Trade) -> dict:
    """Convert Trade dataclass to a JSON-serializable dict."""
    return asdict(trade)


def _dict_to_trade(d: dict) -> Trade:
    """Reconstruct Trade from a dict loaded from JSON."""
    return Trade(**d)


def _compute_resolve_boundary(now_ts: float) -> tuple[str, float]:
    """
    Calculate the end of the current 15-minute boundary.
    Markets align to clock boundaries (e.g. :00, :15, :30, :45).
    Returns (iso_string, unix_timestamp).
    """
    interval = MARKET_INTERVAL_SECS
    boundary_ts = math.ceil(now_ts / interval) * interval
    boundary_dt = datetime.fromtimestamp(boundary_ts, tz=timezone.utc)
    return boundary_dt.isoformat(), boundary_ts


class TradeLogger:
    """
    Logs trades to a JSON file, tracks pending resolutions,
    and computes session statistics.
    """

    def __init__(self, log_file: str = "gabagool_trades.json"):
        self._log_file = Path(log_file)
        self._trades: list[Trade] = []
        self._load()
        logger.info(
            "TradeLogger initialised | file={} | loaded {} trades ({} pending)",
            self._log_file,
            len(self._trades),
            self.get_pending_count(),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_trade(
        self,
        strategy: str,
        token_side: str,
        price: float,
        quantity: float,
        cost_usd: float,
        market_slug: str,
        is_paper: bool = True,
        spread_pair_id: Optional[str] = None,
    ) -> str:
        """
        Record a new trade. Returns the trade_id.

        resolve_at is auto-calculated from the current 15-minute boundary.
        """
        now = time.time()
        resolve_at_iso, resolve_at_ts = _compute_resolve_boundary(now)

        trade_id = str(uuid.uuid4())[:12]

        trade = Trade(
            trade_id=trade_id,
            timestamp=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            strategy=strategy,
            token_side=token_side,
            price=price,
            quantity=quantity,
            cost_usd=cost_usd,
            market_slug=market_slug,
            resolve_at=resolve_at_iso,
            resolve_at_ts=resolve_at_ts,
            is_paper=is_paper,
            spread_pair_id=spread_pair_id,
        )

        self._trades.append(trade)
        self._save()

        logger.info(
            "Trade recorded | id={} strategy={} side={} price={:.4f} qty={:.2f} cost=${:.2f} resolves={}",
            trade_id,
            strategy,
            token_side,
            price,
            quantity,
            cost_usd,
            resolve_at_iso,
        )
        return trade_id

    def record_spread_pair(
        self,
        yes_price: float,
        no_price: float,
        quantity: float,
        market_slug: str,
        is_paper: bool = True,
    ) -> tuple[str, str]:
        """
        Record a spread capture trade pair (YES + NO).
        Returns (yes_trade_id, no_trade_id).
        """
        pair_id = str(uuid.uuid4())[:12]
        yes_cost = yes_price * quantity
        no_cost = no_price * quantity

        yes_id = self.record_trade(
            strategy="spread_capture",
            token_side="YES",
            price=yes_price,
            quantity=quantity,
            cost_usd=yes_cost,
            market_slug=market_slug,
            is_paper=is_paper,
            spread_pair_id=pair_id,
        )
        no_id = self.record_trade(
            strategy="spread_capture",
            token_side="NO",
            price=no_price,
            quantity=quantity,
            cost_usd=no_cost,
            market_slug=market_slug,
            is_paper=is_paper,
            spread_pair_id=pair_id,
        )
        return yes_id, no_id

    def check_resolutions(self, current_yes_price: Optional[float] = None) -> int:
        """
        Check if any pending trades have resolved based on current time and price.
        Returns the number of trades resolved this call.
        """
        now = time.time()
        resolved_count = 0

        for trade in self._trades:
            if trade.outcome != "PENDING":
                continue

            # Not yet past grace period
            if now < trade.resolve_at_ts + RESOLUTION_GRACE_SECS:
                continue

            # No price available -- can we mark unknown?
            if current_yes_price is None:
                if now > trade.resolve_at_ts + RESOLUTION_UNKNOWN_SECS:
                    self._resolve_trade(trade, "UNKNOWN", None)
                    resolved_count += 1
                continue

            trade.resolved_price = current_yes_price

            # Determine outcome based on final YES price
            if current_yes_price > YES_WIN_THRESHOLD:
                # YES won
                if trade.token_side == "YES":
                    self._resolve_trade(trade, "WIN", current_yes_price)
                else:
                    self._resolve_trade(trade, "LOSS", current_yes_price)
                resolved_count += 1

            elif current_yes_price < NO_WIN_THRESHOLD:
                # NO won
                if trade.token_side == "NO":
                    self._resolve_trade(trade, "WIN", current_yes_price)
                else:
                    self._resolve_trade(trade, "LOSS", current_yes_price)
                resolved_count += 1

            else:
                # Price is ambiguous -- wait or mark unknown
                if now > trade.resolve_at_ts + RESOLUTION_UNKNOWN_SECS:
                    self._resolve_trade(trade, "UNKNOWN", current_yes_price)
                    resolved_count += 1

        if resolved_count > 0:
            self._save()
            logger.info("Resolved {} trades this cycle", resolved_count)

        return resolved_count

    def get_pending_count(self) -> int:
        """Number of trades awaiting resolution."""
        return sum(1 for t in self._trades if t.outcome == "PENDING")

    def get_pending_trades(self) -> list[Trade]:
        """Return all trades still awaiting resolution."""
        return [t for t in self._trades if t.outcome == "PENDING"]

    def get_summary(self) -> dict:
        """
        Get session summary with win rate, net P&L, ROI, and per-strategy breakdown.
        """
        resolved = [t for t in self._trades if t.outcome in ("WIN", "LOSS")]
        wins = [t for t in resolved if t.outcome == "WIN"]
        losses = [t for t in resolved if t.outcome == "LOSS"]

        total_cost = sum(t.cost_usd for t in self._trades)
        net_pnl = sum(t.pnl for t in self._trades)
        win_rate = len(wins) / len(resolved) if resolved else 0.0
        roi = (net_pnl / total_cost * 100) if total_cost > 0 else 0.0

        # Per-strategy breakdown
        strategies: dict[str, dict] = {}
        for t in self._trades:
            s = t.strategy
            if s not in strategies:
                strategies[s] = {
                    "total": 0,
                    "wins": 0,
                    "losses": 0,
                    "pending": 0,
                    "unknown": 0,
                    "net_pnl": 0.0,
                    "total_cost": 0.0,
                }
            bucket = strategies[s]
            bucket["total"] += 1
            bucket["total_cost"] += t.cost_usd
            bucket["net_pnl"] += t.pnl
            if t.outcome == "WIN":
                bucket["wins"] += 1
            elif t.outcome == "LOSS":
                bucket["losses"] += 1
            elif t.outcome == "PENDING":
                bucket["pending"] += 1
            else:
                bucket["unknown"] += 1

        for s_data in strategies.values():
            s_resolved = s_data["wins"] + s_data["losses"]
            s_data["win_rate"] = (
                s_data["wins"] / s_resolved if s_resolved > 0 else 0.0
            )

        return {
            "total_trades": len(self._trades),
            "resolved": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "pending": self.get_pending_count(),
            "unknown": sum(1 for t in self._trades if t.outcome == "UNKNOWN"),
            "win_rate": win_rate,
            "net_pnl": round(net_pnl, 4),
            "total_cost": round(total_cost, 4),
            "roi_pct": round(roi, 2),
            "by_strategy": strategies,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_trade(
        self, trade: Trade, outcome: str, resolved_price: Optional[float]
    ) -> None:
        """Apply resolution to a single trade."""
        trade.outcome = outcome
        trade.resolved_price = resolved_price

        if outcome == "WIN":
            trade.pnl = round(trade.quantity * 1.0 - trade.cost_usd, 6)
        elif outcome == "LOSS":
            trade.pnl = round(0.0 - trade.cost_usd, 6)
        else:
            trade.pnl = 0.0

        logger.info(
            "Trade resolved | id={} outcome={} pnl=${:.4f} side={} price_at_resolve={}",
            trade.trade_id,
            outcome,
            trade.pnl,
            trade.token_side,
            resolved_price,
        )

    def _save(self) -> None:
        """Persist all trades to the JSON file."""
        data = [_trade_to_dict(t) for t in self._trades]
        self._log_file.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        """Load trades from the JSON file if it exists."""
        if not self._log_file.exists():
            self._trades = []
            return

        try:
            raw = json.loads(self._log_file.read_text())
            self._trades = [_dict_to_trade(d) for d in raw]
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Failed to load trade log, starting fresh: {}", exc)
            self._trades = []
