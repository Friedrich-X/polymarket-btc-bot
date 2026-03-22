# Gabagool

Hybrid Momentum + Spread Capture trading bot for Polymarket BTC 15-minute markets. It watches Binance BTC price in real-time and trades Polymarket binary options before the market reprices.

## Setup

```bash
cd Polymarket-BTC-15-Minute-Trading-Bot
python3 -m venv venv
source venv/bin/activate
pip install -r gabagool/requirements.txt
```

Copy your `.env` file with Polymarket credentials into the project root (same credentials as the main bot).

## Usage

```bash
# Paper trading (default -- no real money)
python3 -m gabagool.gabagool_bot

# Live trading
python3 -m gabagool.gabagool_bot --live

# Custom config / budget
python3 -m gabagool.gabagool_bot --live --budget 60 --config gabagool/config.json
```

## Configuration

All settings live in `gabagool/config.json`:

### `momentum`

| Field                   | Default | Description                                     |
| ----------------------- | ------- | ----------------------------------------------- |
| `enabled`               | `true`  | Enable/disable momentum strategy                |
| `lookback_secs`         | `3`     | Seconds of BTC price history to measure delta   |
| `entry_min_delta`       | `0.003` | Minimum BTC price change (%) to trigger a trade |
| `cooldown_secs`         | `30`    | Wait time between momentum trades               |
| `order_size_usd`        | `5.0`   | USD size per momentum order                     |
| `order_type`            | `"FOK"` | Order type (Fill-or-Kill)                       |
| `max_trades_per_window` | `5`     | Max momentum trades per 15-min market window    |

### `spread_capture`

| Field                 | Default | Description                                          |
| --------------------- | ------- | ---------------------------------------------------- |
| `enabled`             | `true`  | Enable/disable spread capture strategy               |
| `spread_threshold`    | `0.96`  | Max combined YES+NO price to enter (< 1.00 = profit) |
| `order_size_usd`      | `5.0`   | USD size per spread leg                              |
| `order_type`          | `"GTC"` | Order type (Good-Til-Cancelled)                      |
| `max_imbalance_ratio` | `1.5`   | Max YES/NO price ratio to avoid lopsided spreads     |
| `cooldown_secs`       | `10`    | Wait time between spread trades                      |

### `general`

| Field                  | Default                  | Description                         |
| ---------------------- | ------------------------ | ----------------------------------- |
| `budget_usd`           | `60.0`                   | Total trading budget in USD         |
| `market_interval_secs` | `900`                    | Market window length (900 = 15 min) |
| `log_file`             | `"gabagool_trades.json"` | Path to trade log file              |
| `poll_interval_ms`     | `500`                    | How often to poll for prices (ms)   |

## Strategies

### Momentum

Monitors BTC price on Binance via WebSocket. When BTC moves significantly within a short window (`lookback_secs`), it buys the corresponding Polymarket YES or NO token before the market reprices. Profits come from being faster than the Polymarket order book.

### Spread Capture

Watches the Polymarket order book for mispricing. When YES + NO can be bought for less than $1.00 combined (below `spread_threshold`), it buys both sides. Since one side always pays out $1.00, the difference is risk-free profit. For example, buying YES at $0.47 and NO at $0.48 costs $0.95 and guarantees $1.00 back.

## Raspberry Pi Deployment

```bash
# Copy the service file
sudo cp gabagool/gabagool.service /etc/systemd/system/

# Edit paths in the service file if your install location differs
sudo nano /etc/systemd/system/gabagool.service

# Reload systemd, enable, and start
sudo systemctl daemon-reload
sudo systemctl enable gabagool
sudo systemctl start gabagool

# Check status
sudo systemctl status gabagool

# Follow logs
journalctl -u gabagool -f
```

To stop the bot:

```bash
sudo systemctl stop gabagool
```

## Trade Logs

All trades are logged to `gabagool_trades.json` (configurable via `general.log_file`). Each entry contains:

| Field            | Description                                    |
| ---------------- | ---------------------------------------------- |
| `trade_id`       | Unique 12-char identifier                      |
| `timestamp`      | ISO 8601 UTC timestamp of trade entry          |
| `strategy`       | `"momentum"` or `"spread_capture"`             |
| `token_side`     | `"YES"` or `"NO"`                              |
| `price`          | Entry price paid per token                     |
| `quantity`       | Number of tokens bought                        |
| `cost_usd`       | Total USD cost of the trade                    |
| `market_slug`    | Polymarket market identifier                   |
| `resolve_at`     | ISO 8601 timestamp when the 15-min window ends |
| `outcome`        | `"PENDING"`, `"WIN"`, `"LOSS"`, or `"UNKNOWN"` |
| `pnl`            | Profit/loss in USD (0 until resolved)          |
| `resolved_price` | Final YES token price at resolution            |
| `is_paper`       | `true` for paper trades, `false` for live      |
| `spread_pair_id` | Links YES/NO legs of spread capture trades     |

The bot automatically resolves pending trades at the end of each 15-minute window and updates outcomes and P&L.

## Disclaimer

This is experimental software. It trades real money on Polymarket when run in live mode. Use at your own risk. This is not financial advice. The authors are not responsible for any losses incurred. Always start with paper trading to verify behavior before risking real funds.
