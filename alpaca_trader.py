"""
Alpaca Trader — handles Gold, Quantum Computing stocks alongside Kraken crypto.
Runs during US market hours (Mon-Fri 15:30-22:00 DE time).
Uses same momentum strategy as crypto bot.
"""
import pandas as pd
import ta
from alpaca_exchange import AlpacaExchange, SYMBOLS, ASSETS
from trade_logger import TradeLogger
from config import Config


class AlpacaTrader:
    def __init__(self):
        self.exchange = AlpacaExchange()
        self.logger = TradeLogger(log_dir="logs/alpaca")
        self.open_positions = {}  # tracked locally for SL/TP

        # Risk per trade: 20% of alpaca balance
        self.risk_pct = 0.20
        self.stop_loss_pct = Config.STOP_LOSS_PCT
        self.take_profit_pct = Config.TAKE_PROFIT_PCT

    def run_cycle(self):
        if not self.exchange.is_market_open():
            print("  [Alpaca] Market closed — skipping")
            return

        balance = self.exchange.get_balance()
        print(f"  [Alpaca] Balance: {balance:.2f} USD")

        # Check exits on open positions
        self._check_exits()

        # Analyze each asset
        for key, asset in ASSETS.items():
            symbol = asset["symbol"]
            print(f"  [Alpaca] Analyzing {symbol} ({asset['name']})...")

            if symbol in self.open_positions:
                continue

            df = self.exchange.get_ohlcv(symbol, "15m", limit=60)
            if df.empty or len(df) < 20:
                print(f"    Not enough data for {symbol}")
                continue

            signal = self._analyze(df, symbol)

            if signal == "buy":
                ticker = self.exchange.get_ticker(symbol)
                if not ticker:
                    continue
                price = ticker["last"]
                position_value = balance * self.risk_pct
                qty = position_value / price

                if qty < 0.01:
                    print(f"    Skip {symbol}: qty too small")
                    continue

                result = self.exchange.place_order(symbol, "buy", qty)
                if result["status"] == "ok":
                    self.open_positions[symbol] = {
                        "entry_price": price,
                        "qty": qty,
                        "stop_loss": price * (1 - self.stop_loss_pct),
                        "take_profit": price * (1 + self.take_profit_pct),
                        "asset": asset["name"],
                    }
                    print(f"    >> BUY {qty:.4f} {symbol} @ {price:.2f} USD")
                    self.logger.log_trade(
                        pair=symbol, side="buy", volume=qty, price=price,
                        cost=qty * price, fee=0, mode=Config.TRADING_MODE,
                        strategy="alpaca_momentum",
                        signal_reason="momentum signal",
                        balance_after=balance - qty * price,
                    )

    def _analyze(self, df: pd.DataFrame, symbol: str) -> str:
        close = df["close"]

        if len(close) < 14:
            return "hold"

        rsi = ta.momentum.RSIIndicator(close, window=10).rsi().iloc[-1]
        ema_f = ta.trend.EMAIndicator(close, window=5).ema_indicator()
        ema_s = ta.trend.EMAIndicator(close, window=13).ema_indicator()
        macd = ta.trend.MACD(close).macd_diff().iloc[-1]

        cross_up = ema_f.iloc[-2] <= ema_s.iloc[-2] and ema_f.iloc[-1] > ema_s.iloc[-1]
        bullish = ema_f.iloc[-1] > ema_s.iloc[-1]

        # Buy conditions
        if rsi < 35 and bullish:
            print(f"    RSI {rsi:.0f} oversold + bullish EMA")
            return "buy"
        if cross_up and macd > 0:
            print(f"    EMA cross up + MACD positive")
            return "buy"
        if rsi < 45 and bullish and macd > 0:
            print(f"    RSI {rsi:.0f} low + bullish")
            return "buy"

        return "hold"

    def _check_exits(self):
        positions = self.exchange.get_positions()

        for symbol in list(self.open_positions.keys()):
            pos = self.open_positions[symbol]
            ticker = self.exchange.get_ticker(symbol)
            if not ticker:
                continue

            price = ticker["last"]
            exit_reason = None

            if price <= pos["stop_loss"]:
                exit_reason = "stop_loss"
            elif price >= pos["take_profit"]:
                exit_reason = "take_profit"

            if exit_reason:
                qty = positions.get(symbol, {}).get("qty", pos["qty"])
                result = self.exchange.place_order(symbol, "sell", qty)
                if result["status"] == "ok":
                    pnl = (price - pos["entry_price"]) * qty
                    print(f"  [Alpaca] >> {exit_reason.upper()} {symbol} P&L: {pnl:+.2f} USD")
                    self.logger.log_trade(
                        pair=symbol, side="sell", volume=qty, price=price,
                        cost=qty * price, fee=0, mode=Config.TRADING_MODE,
                        strategy="alpaca_momentum",
                        signal_reason=exit_reason,
                        balance_after=0,
                    )
                    del self.open_positions[symbol]
