import pandas as pd
import ta
import time
from config import Config


class Signal:
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class MomentumStrategy:
    """RSI + EMA crossover with aggressive thresholds."""

    def analyze(self, df: pd.DataFrame) -> dict:
        if df.empty or len(df) < Config.EMA_SLOW + 5:
            return {"signal": Signal.HOLD, "reason": "Not enough data"}

        close = df["close"]

        rsi = ta.momentum.RSIIndicator(close, window=Config.RSI_PERIOD).rsi()
        current_rsi = rsi.iloc[-1]

        ema_f = ta.trend.EMAIndicator(close, window=Config.EMA_FAST).ema_indicator()
        ema_s = ta.trend.EMAIndicator(close, window=Config.EMA_SLOW).ema_indicator()

        bullish = ema_f.iloc[-1] > ema_s.iloc[-1]
        bearish = ema_f.iloc[-1] < ema_s.iloc[-1]
        cross_up = ema_f.iloc[-2] <= ema_s.iloc[-2] and ema_f.iloc[-1] > ema_s.iloc[-1]
        cross_down = ema_f.iloc[-2] >= ema_s.iloc[-2] and ema_f.iloc[-1] < ema_s.iloc[-1]

        # MACD for extra confirmation
        macd = ta.trend.MACD(close)
        macd_hist = macd.macd_diff().iloc[-1]

        # Volume spike detection
        avg_vol = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else 0
        current_vol = df["volume"].iloc[-1]
        vol_spike = avg_vol > 0 and current_vol > avg_vol * 1.8

        # Breakout: new 20-period high with volume
        high_20 = df["close"].rolling(20).max().iloc[-2] if len(df) >= 20 else 0
        breakout = close.iloc[-1] > high_20 and vol_spike

        # Breakdown: new 20-period low with volume
        low_20 = df["close"].rolling(20).min().iloc[-2] if len(df) >= 20 else 999999
        breakdown = close.iloc[-1] < low_20 and vol_spike

        signal = Signal.HOLD
        reasons = []

        # Signalstärke bestimmt Hebel
        # 3 Bedingungen erfüllt = starkes Signal → 2x Hebel
        # 2 Bedingungen       = normales Signal → 1x
        leverage = 1

        # Breakout buy (stärkstes Signal → 2x Hebel)
        if breakout and bullish:
            signal = Signal.BUY
            reasons = ["Breakout new high", "volume spike"]
            leverage = 2

        # RSI oversold + bullish + MACD → 2x Hebel
        elif current_rsi < Config.RSI_OVERSOLD and bullish and macd_hist > 0:
            signal = Signal.BUY
            reasons = [f"RSI {current_rsi:.0f} oversold", "EMA bullish", "MACD pos"]
            leverage = 2

        # RSI oversold + bullish (kein MACD) → 1x
        elif current_rsi < Config.RSI_OVERSOLD and bullish:
            signal = Signal.BUY
            reasons = [f"RSI {current_rsi:.0f} oversold", "EMA bullish"]
            leverage = 1

        # EMA cross up + MACD positiv → 1.5x (abgerundet auf 1 für Sicherheit)
        elif cross_up and macd_hist > 0:
            signal = Signal.BUY
            reasons = ["EMA cross up", "MACD pos"]
            leverage = 1

        # EMA cross up ohne MACD → 1x
        elif cross_up:
            signal = Signal.BUY
            reasons = ["EMA cross up", f"MACD {'pos' if macd_hist > 0 else 'neg'}"]
            leverage = 1

        # RSI low + bullish (relaxed) → 1x
        elif current_rsi < 45 and bullish and macd_hist > 0:
            signal = Signal.BUY
            reasons = [f"RSI {current_rsi:.0f} low", "EMA bullish", "MACD pos"]
            leverage = 1

        # Short: Breakdown — neues 20-Perioden-Tief + Volumen-Spike → 2x Short
        elif breakdown and bearish:
            signal = Signal.SELL
            reasons = ["Breakdown new low", "volume spike"]
            leverage = 2

        # Short: RSI überkauft + bearish + MACD negativ → 2x Short
        elif current_rsi > Config.RSI_OVERBOUGHT and bearish and macd_hist < 0:
            signal = Signal.SELL
            reasons = [f"RSI {current_rsi:.0f} overbought", "EMA bearish", "MACD neg"]
            leverage = 2

        # Short: EMA cross down + MACD negativ + bearish → 1x Short
        elif cross_down and macd_hist < 0 and bearish:
            signal = Signal.SELL
            reasons = ["EMA cross down", "MACD neg", "bearish"]
            leverage = 1

        # Kein Short bei niedrigem RSI — Coin bereits überverkauft, Bounce wahrscheinlich

        return {
            "signal": signal,
            "reason": " + ".join(reasons) if reasons else "No signal",
            "rsi": round(current_rsi, 2),
            "price": round(close.iloc[-1], 2),
            "strategy": "momentum",
            "leverage": leverage,
            "short": signal == Signal.SELL and not any(p == "momentum" for p in []),
        }


class GridStrategy:
    """Grid trading: place buy/sell orders at regular intervals."""

    def __init__(self):
        self.grids = {}  # symbol -> grid state

    def analyze(self, df: pd.DataFrame, symbol: str) -> dict:
        if df.empty or len(df) < 20:
            return {"signal": Signal.HOLD, "reason": "Not enough data", "strategy": "grid"}

        close = df["close"]
        current_price = close.iloc[-1]

        # Initialize grid for this symbol
        if symbol not in self.grids:
            high = close.rolling(20).max().iloc[-1]
            low = close.rolling(20).min().iloc[-1]
            spread = (high - low) / high

            # Only grid trade if the range is reasonable (2-8%)
            if spread < 0.02 or spread > 0.15:
                return {"signal": Signal.HOLD, "reason": f"Range {spread*100:.1f}% not ideal for grid",
                        "strategy": "grid"}

            grid_step = (high - low) / Config.GRID_LEVELS
            self.grids[symbol] = {
                "high": high,
                "low": low,
                "step": grid_step,
                "last_action_price": current_price,
                "last_action": None,
            }

        grid = self.grids[symbol]
        price_diff = current_price - grid["last_action_price"]
        step = grid["step"]

        signal = Signal.HOLD
        reasons = []

        # Buy when price drops by one grid level
        if price_diff <= -step and current_price >= grid["low"]:
            signal = Signal.BUY
            reasons = [f"Grid buy at {current_price:.2f}", f"dropped {abs(price_diff):.2f}"]
            grid["last_action_price"] = current_price
            grid["last_action"] = "buy"

        # Sell when price rises by one grid level
        elif price_diff >= step and current_price <= grid["high"]:
            signal = Signal.SELL
            reasons = [f"Grid sell at {current_price:.2f}", f"rose {price_diff:.2f}"]
            grid["last_action_price"] = current_price
            grid["last_action"] = "sell"

        return {
            "signal": signal,
            "reason": " + ".join(reasons) if reasons else f"In grid range ({grid['low']:.0f}-{grid['high']:.0f})",
            "price": round(current_price, 2),
            "strategy": "grid",
        }

    def reset_grid(self, symbol: str):
        """Reset grid when market conditions change significantly."""
        self.grids.pop(symbol, None)


class DCAStrategy:
    """Dollar Cost Averaging: buy at regular intervals, more when price is low."""

    def __init__(self):
        self.last_buy_time = {}

    def analyze(self, df: pd.DataFrame, symbol: str) -> dict:
        if df.empty:
            return {"signal": Signal.HOLD, "reason": "No data", "strategy": "dca"}

        current_price = df["close"].iloc[-1]
        now = time.time()

        # Check if enough time passed since last DCA buy
        last = self.last_buy_time.get(symbol, 0)
        minutes_since = (now - last) / 60

        if minutes_since < Config.DCA_INTERVAL_MINUTES:
            remaining = Config.DCA_INTERVAL_MINUTES - minutes_since
            return {"signal": Signal.HOLD,
                    "reason": f"DCA wait {remaining:.0f}min",
                    "strategy": "dca"}

        # Calculate RSI to adjust DCA amount
        rsi = 50
        if len(df) >= 14:
            rsi = ta.momentum.RSIIndicator(df["close"], window=14).rsi().iloc[-1]

        # Only DCA when RSI is not overbought
        if rsi > 55:
            return {"signal": Signal.HOLD,
                    "reason": f"DCA skipped (RSI {rsi:.0f} too high)",
                    "strategy": "dca"}

        # Buy more aggressively when RSI is low (cheaper prices)
        multiplier = 1.0
        reason = "DCA regular buy"
        if rsi < 30:
            multiplier = 2.0
            reason = f"DCA heavy buy (RSI {rsi:.0f} oversold)"
        elif rsi < 40:
            multiplier = 1.5
            reason = f"DCA extra buy (RSI {rsi:.0f} low)"

        self.last_buy_time[symbol] = now

        return {
            "signal": Signal.BUY,
            "reason": reason,
            "price": round(current_price, 2),
            "strategy": "dca",
            "dca_multiplier": multiplier,
        }
