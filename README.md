# Crypto Trading Bot v2

Multi-strategy, multi-coin, AI-powered trading bot for Kraken.

## Features
- **Multi-Coin Scanner**: Scans top 30 coins, auto-picks the 5 best opportunities
- **4 Strategies running simultaneously**:
  - Momentum (RSI + EMA + MACD)
  - Grid Trading (buy low / sell high in a range)
  - DCA (Dollar Cost Averaging with RSI-adjusted sizing)
  - AI News Sentiment (Claude analyzes crypto news in real-time)
- **Aggressive risk mode**: 15% risk per trade, 6% stop-loss, 10% take-profit
- **Volume spike detection**: Catches unusual activity early
- **Trade logging**: FIFO-compliant CSV for Spanish taxes
- **Telegram alerts** (optional)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

## Run

```bash
# Paper trading (fake money, real market data)
python main.py --mode paper

# Live trading (real money!)
python main.py --mode live
```

## API Keys needed
1. **Kraken**: Settings -> API -> Create Key (Trade + Query only, NO withdrawal)
2. **Anthropic** (optional, for news sentiment): console.anthropic.com -> API Keys
3. **Telegram** (optional): Talk to @BotFather -> /newbot

## How it works

Every 60 seconds:
1. Scans all EUR pairs on Kraken for volume spikes, RSI signals, momentum
2. Picks the top 5 coins with the best opportunity score
3. Checks crypto news via RSS feeds + Claude AI for sentiment signals
4. Runs all 4 strategies on each target coin
5. Executes the highest-priority signal (sentiment > momentum > grid > dca)
6. Monitors open positions for stop-loss / take-profit exits
7. Logs everything to CSV for tax season

## Risk Settings (aggressive)
- 15% of balance risked per trade
- Up to 5 simultaneous positions
- Stop-loss: 6% (9% for sentiment trades)
- Take-profit: 10% (15% for sentiment trades)
- DCA: 15% stop, 20% target (longer horizon)

## Disable strategies
Edit .env:
```
ACTIVE_STRATEGIES=momentum,grid
```
