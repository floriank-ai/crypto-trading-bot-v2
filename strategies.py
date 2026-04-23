import pandas as pd
import ta
import time
from config import Config


class Signal:
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class MomentumStrategy:
    """Strict momentum: only high-conviction signals with ADX trend filter."""

    def analyze(self, df: pd.DataFrame) -> dict:
        if df.empty or len(df) < 30:
            return {"signal": Signal.HOLD, "reason": "Not enough data"}

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        rsi = ta.momentum.RSIIndicator(close, window=Config.RSI_PERIOD).rsi()
        current_rsi = rsi.iloc[-1]

        ema_f = ta.trend.EMAIndicator(close, window=Config.EMA_FAST).ema_indicator()
        ema_s = ta.trend.EMAIndicator(close, window=Config.EMA_SLOW).ema_indicator()
        bullish = ema_f.iloc[-1] > ema_s.iloc[-1]
        bearish = ema_f.iloc[-1] < ema_s.iloc[-1]

        macd = ta.trend.MACD(close)
        macd_hist = macd.macd_diff().iloc[-1]

        # ADX: only trade on a real trend (> 20)
        adx = ta.trend.ADXIndicator(high, low, close, window=14).adx()
        trending = adx.iloc[-1] > 20

        avg_vol = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else 0
        # Volume filter back to 1.8x: 1.3x produced 4/4 Loser SHORTs on
        # junk alts in a BULLISH market (Log 17.04, -20 EUR in 6h). False breakouts
        # came through too cheaply. 1.8x = real conviction, fewer but better.
        vol_spike = avg_vol > 0 and df["volume"].iloc[-1] > avg_vol * 1.8

        high_20 = close.rolling(20).max().iloc[-2] if len(df) >= 21 else 0
        low_20  = close.rolling(20).min().iloc[-2] if len(df) >= 21 else 999999
        breakout  = close.iloc[-1] > high_20 and vol_spike
        breakdown = close.iloc[-1] < low_20  and vol_spike

        # Bollinger Bands: filter overstretched entries
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_upper  = bb.bollinger_hband().iloc[-1]
        bb_middle = bb.bollinger_mavg().iloc[-1]
        bb_lower  = bb.bollinger_lband().iloc[-1]
        price_now = close.iloc[-1]

        signal = Signal.HOLD
        reasons = []
        leverage = 1

        if trending:
            # Long: breakout new high + volume
            # Block if >2% above upper BB (overstretched, false-breakout risk)
            if breakout and bullish and price_now <= bb_upper * 1.02:
                signal = Signal.BUY
                reasons = ["Breakout new high + volume spike"]
                leverage = 2

            # Long: RSI extreme oversold + EMA bullish + MACD positive
            # Back to 32 (was loosened to 38 -> too early / lower quality).
            # Only buy when price is still below/at the BB midpoint (really cheap)
            elif current_rsi < 32 and bullish and macd_hist > 0 and price_now <= bb_middle:
                signal = Signal.BUY
                reasons = [f"RSI {current_rsi:.0f} extreme oversold + MACD pos"]
                leverage = 2

            # Short: breakdown new low + volume
            # Block if >2% below lower BB (overstretched, bounce risk)
            elif breakdown and bearish and price_now >= bb_lower * 0.98:
                signal = Signal.SELL
                reasons = ["Breakdown new low + volume spike"]
                leverage = 2

            # Short: RSI extreme overbought + EMA bearish + MACD negative
            # Back to 68 (was loosened to 62 -> too early / lower quality).
            # Only short when price is still above/at the BB midpoint
            elif current_rsi > 68 and bearish and macd_hist < 0 and price_now >= bb_middle:
                signal = Signal.SELL
                reasons = [f"RSI {current_rsi:.0f} extreme overbought + MACD neg"]
                leverage = 2

        return {
            "signal": signal,
            "reason": " + ".join(reasons) if reasons else "No signal",
            "rsi": round(current_rsi, 2),
            "price": round(close.iloc[-1], 2),
            "strategy": "momentum",
            "leverage": leverage,
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


class GainerStrategy:
    """Entry filter for top gainers: avoid buying at the absolute peak.

    Log 22.04.2026 learned: SPK +50% @ RSI 79 verlor -9.02EUR (86% des Tagesverlustes).
    Klassisches "Buying the top". Fixes: Max-Gain-Cap 40%, RSI-Cap 72, green-candle-req.
    """

    def analyze(self, df: pd.DataFrame, gain_24h: float = 0) -> dict:
        if df.empty or len(df) < 20:
            return {"signal": Signal.HOLD, "reason": "Not enough data", "strategy": "gainer"}

        close = df["close"]
        open_ = df["open"]

        # Max-24h-Gain-Cap: >40% = Pump vermutlich gelaufen, Einstiegsrisiko zu hoch.
        # SPK war +50% → sofort -9EUR verloren. NEIRO +38%, SPX +16% waren erfolgreich.
        if gain_24h >= Config.GAINER_MAX_GAIN_24H:
            return {"signal": Signal.HOLD,
                    "reason": f"Gain +{gain_24h:.0f}% zu hoch (>={Config.GAINER_MAX_GAIN_24H:.0f}%) — Pump gelaufen",
                    "strategy": "gainer"}

        # RSI-Cap 72 (war 88): Gainer-Coins haben naturgemäß hohen RSI, aber >72 =
        # overbought nach Standard-Definition. SPK RSI 79 → sofort gedumpt. NEIRO 60,
        # SPX 54/49/58 → alle erfolgreich = sicherer Bereich.
        rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        if rsi > Config.GAINER_RSI_MAX:
            return {"signal": Signal.HOLD,
                    "reason": f"RSI {rsi:.0f} overbought (>{Config.GAINER_RSI_MAX:.0f}) — top-buying-risk",
                    "strategy": "gainer"}

        # EMA trend: must be bullish
        ema_f = ta.trend.EMAIndicator(close, window=9).ema_indicator().iloc[-1]
        ema_s = ta.trend.EMAIndicator(close, window=21).ema_indicator().iloc[-1]
        if ema_f <= ema_s:
            return {"signal": Signal.HOLD,
                    "reason": "EMA not bullish",
                    "strategy": "gainer"}

        # Letzte 15m-Kerze muss grün sein — SPK war bereits im 15m-Rückfall als Bot kaufte.
        # Grüne Kerze = Momentum aktuell positiv, nicht bereits am Umkehren.
        last_close = close.iloc[-1]
        last_open = open_.iloc[-1]
        if last_close <= last_open:
            return {"signal": Signal.HOLD,
                    "reason": "Letzte 15m rot — Momentum dreht bereits",
                    "strategy": "gainer"}

        # Volume: must still have elevated volume (not fading)
        avg_vol = df["volume"].rolling(20).mean().iloc[-1]
        if avg_vol > 0 and df["volume"].iloc[-1] < avg_vol * 0.7:
            return {"signal": Signal.HOLD,
                    "reason": "Volume fading — momentum dying",
                    "strategy": "gainer"}

        return {
            "signal": Signal.BUY,
            "reason": f"Gainer +{gain_24h:.0f}% 24h | RSI {rsi:.0f} | EMA bullish | 15m grün | vol ok",
            "rsi": round(rsi, 2),
            "price": round(close.iloc[-1], 8),
            "strategy": "gainer",
            "leverage": 1,
        }


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
