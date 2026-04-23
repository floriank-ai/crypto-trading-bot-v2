#!/usr/bin/env python3
"""
Vergleicht alle Filter-Varianten gegen die BB-Baseline.
Testet: MultiTimeframe, VWAP, CandlePattern, ATR-Sizing, ConsecLoss, TimeFilter + All Combined.
"""
import ccxt
import pandas as pd
import ta as ta_lib
import time
from datetime import datetime, timedelta, timezone
from config import Config
from strategies import Signal

SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "ADA/EUR", "XRP/EUR"]
INITIAL_CAPITAL = 1000.0
POSITION_SIZE_PCT = 0.20
STOP_LOSS_PCT = Config.STOP_LOSS_PCT
TAKE_PROFIT_PCT = Config.TAKE_PROFIT_PCT
FEE_PCT = 0.0026
WARMUP = 50
MONTHS = 2


# ── Daten laden ─────────────────────────────────────────────────────────────

_binance = None

def get_binance():
    global _binance
    if _binance is None:
        _binance = ccxt.binance({"enableRateLimit": True})
        _binance.load_markets()
    return _binance


def fetch_binance(symbol, months=3):
    base = symbol.split("/")[0]
    sym = f"{base}/USDT"
    try:
        b = get_binance()
        if sym not in b.symbols:
            return pd.DataFrame()
        total_candles = months * 30 * 24 * 4  # max candles
        since = int((datetime.now(timezone.utc) - timedelta(days=months * 30)).timestamp() * 1000)
        rows = []
        while len(rows) < total_candles:
            batch = b.fetch_ohlcv(sym, "15m", since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            since = batch[-1][0] + 1
            if len(batch) < 1000:
                break
            time.sleep(0.15)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows[:total_candles], columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        return df[df["close"] > 0].reset_index(drop=True)
    except Exception as e:
        print(f"  Fehler {symbol}: {e}")
        return pd.DataFrame()


# ── Basis-Strategie (BB-Filter = aktueller Stand) ───────────────────────────

def get_signal_bb(window):
    close = window["close"]
    high = window["high"]
    low = window["low"]
    if len(close) < 30:
        return Signal.HOLD, None

    rsi = ta_lib.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
    ema_f = ta_lib.trend.EMAIndicator(close, window=9).ema_indicator().iloc[-1]
    ema_s = ta_lib.trend.EMAIndicator(close, window=21).ema_indicator().iloc[-1]
    bullish = ema_f > ema_s
    bearish = ema_f < ema_s
    macd_hist = ta_lib.trend.MACD(close).macd_diff().iloc[-1]
    adx = ta_lib.trend.ADXIndicator(high, low, close, window=14).adx().iloc[-1]
    trending = adx > 20

    avg_vol = window["volume"].rolling(20).mean().iloc[-1]
    vol_spike = avg_vol > 0 and window["volume"].iloc[-1] > avg_vol * 1.8
    high_20 = close.rolling(20).max().iloc[-2] if len(close) >= 21 else 0
    low_20 = close.rolling(20).min().iloc[-2] if len(close) >= 21 else 999999
    breakout = close.iloc[-1] > high_20 and vol_spike
    breakdown = close.iloc[-1] < low_20 and vol_spike

    bb = ta_lib.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper = bb.bollinger_hband().iloc[-1]
    bb_middle = bb.bollinger_mavg().iloc[-1]
    bb_lower = bb.bollinger_lband().iloc[-1]
    price = close.iloc[-1]

    if not trending:
        return Signal.HOLD, None

    if breakout and bullish and price <= bb_upper * 1.02:
        return Signal.BUY, "breakout"
    if rsi < 32 and bullish and macd_hist > 0 and price <= bb_middle:
        return Signal.BUY, "rsi_oversold"
    if breakdown and bearish and price >= bb_lower * 0.98:
        return Signal.SELL, "breakdown"
    if rsi > 68 and bearish and macd_hist < 0 and price >= bb_middle:
        return Signal.SELL, "rsi_overbought"
    return Signal.HOLD, None


# ── Filter-Funktionen ────────────────────────────────────────────────────────

def filter_multiframe(window):
    """1h-Trend muss mit 15m-Signal übereinstimmen."""
    close = window["close"]
    if len(close) < 60:
        return True  # nicht genug Daten → durchlassen
    # Resample to 1h
    df_1h = window.set_index("time").resample("1h")["close"].last().dropna()
    if len(df_1h) < 10:
        return True
    ema_f_1h = ta_lib.trend.EMAIndicator(df_1h, window=9).ema_indicator()
    ema_s_1h = ta_lib.trend.EMAIndicator(df_1h, window=21).ema_indicator()
    return ema_f_1h.iloc[-1], ema_s_1h.iloc[-1]


def filter_vwap(window):
    """Gibt VWAP zurück (rolling über alle verfügbaren Daten des Tages)."""
    df = window.copy()
    df["date"] = df["time"].dt.date
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["cum_tp_vol"] = df.groupby("date")["typical"].transform(
        lambda x: (x * df.loc[x.index, "volume"]).cumsum()
    )
    df["cum_vol"] = df.groupby("date")["volume"].transform("cumsum")
    vwap = (df["cum_tp_vol"] / df["cum_vol"]).iloc[-1]
    return vwap


def filter_candle(window):
    """Gibt Kerzenmuster zurück: 'doji', 'shooting_star', 'hammer', None."""
    last = window.iloc[-1]
    body = abs(last["close"] - last["open"])
    total = last["high"] - last["low"]
    if total == 0:
        return None
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    if body < 0.1 * total:
        return "doji"
    if upper_wick > 2.5 * body and lower_wick < body:
        return "shooting_star"  # bearish reversal
    if lower_wick > 2.5 * body and upper_wick < body:
        return "hammer"  # bullish reversal
    return None


def get_atr_position_pct(window):
    """Kleinere Position bei hoher Volatilität."""
    if len(window) < 15:
        return POSITION_SIZE_PCT
    atr = ta_lib.volatility.AverageTrueRange(
        window["high"], window["low"], window["close"], window=14
    ).average_true_range().iloc[-1]
    price = window["close"].iloc[-1]
    atr_pct = atr / price if price > 0 else 0
    if atr_pct > 0.05:
        return 0.08   # sehr volatil → 8%
    if atr_pct > 0.03:
        return 0.12   # volatil → 12%
    return POSITION_SIZE_PCT  # normal → 20%


def is_low_volume_hour(ts):
    """True wenn Krypto-Volumen sehr niedrig (0-7 Uhr UTC)."""
    return ts.hour < 7


# ── Backtest-Engine ──────────────────────────────────────────────────────────

def run_variant(df, symbol, variant="baseline"):
    balance = INITIAL_CAPITAL
    position = None
    trades = []
    consec_losses = 0

    for i in range(WARMUP, len(df)):
        window = df.iloc[:i + 1]
        price = df["close"].iloc[i]
        ts = df["time"].iloc[i]

        # Exit prüfen
        if position:
            d = position["direction"]
            hit_sl = (d == "long" and price <= position["sl"]) or \
                     (d == "short" and price >= position["sl"])
            hit_tp = (d == "long" and price >= position["tp"]) or \
                     (d == "short" and price <= position["tp"])
            if hit_sl or hit_tp:
                fee = price * position["volume"] * FEE_PCT
                if d == "long":
                    pnl = (price - position["entry"]) * position["volume"] - 2 * fee
                    balance += price * position["volume"] - fee
                else:
                    pnl = (position["entry"] - price) * position["volume"] - 2 * fee
                    balance += position["margin"] + pnl
                exit_type = "sl" if hit_sl else "tp"
                trades.append({"pnl": pnl, "exit": exit_type})
                if pnl < 0:
                    consec_losses += 1
                else:
                    consec_losses = 0
                position = None

        if position is not None or balance < 10:
            continue

        # Basis-Signal holen
        sig, sig_type = get_signal_bb(window)
        if sig == Signal.HOLD:
            continue

        # ── Varianten-Filter ─────────────────────────────────────────────
        if variant == "multiframe":
            result = filter_multiframe(window)
            if isinstance(result, tuple):
                ema_f_1h, ema_s_1h = result
                if sig == Signal.BUY and ema_f_1h < ema_s_1h:
                    continue  # 15m BUY aber 1h bearish → skip
                if sig == Signal.SELL and ema_f_1h > ema_s_1h:
                    continue  # 15m SHORT aber 1h bullish → skip

        elif variant == "vwap":
            vwap = filter_vwap(window)
            if sig == Signal.BUY and price > vwap * 1.005:
                continue  # Kaufen >0.5% über VWAP → zu teuer
            if sig == Signal.SELL and price < vwap * 0.995:
                continue  # Shorten <0.5% unter VWAP → zu billig

        elif variant == "candle":
            pattern = filter_candle(window)
            if pattern == "doji":
                continue  # Unsicherheit → skip beides
            if sig == Signal.BUY and pattern == "shooting_star":
                continue  # Bearish reversal signal → kein Long
            # hammer bestätigt BUY → durchlassen

        elif variant == "atr":
            pos_pct = get_atr_position_pct(window)
            # Position Size wird unten verwendet

        elif variant == "consec":
            if consec_losses >= 2:
                continue  # 2+ Verluste hintereinander → Pause

        elif variant == "timefilter":
            if is_low_volume_hour(ts):
                continue  # 0-7 Uhr UTC → kein Einstieg

        elif variant == "combined":
            # Alle Filter zusammen
            # 1. Multiframe
            result = filter_multiframe(window)
            if isinstance(result, tuple):
                ema_f_1h, ema_s_1h = result
                if sig == Signal.BUY and ema_f_1h < ema_s_1h:
                    continue
                if sig == Signal.SELL and ema_f_1h > ema_s_1h:
                    continue
            # 2. VWAP
            vwap = filter_vwap(window)
            if sig == Signal.BUY and price > vwap * 1.005:
                continue
            if sig == Signal.SELL and price < vwap * 0.995:
                continue
            # 3. Candle
            pattern = filter_candle(window)
            if pattern == "doji":
                continue
            if sig == Signal.BUY and pattern == "shooting_star":
                continue
            # 4. Consec losses
            if consec_losses >= 2:
                continue
            # 5. Time filter
            if is_low_volume_hour(ts):
                continue

        # Position eröffnen
        pos_pct = get_atr_position_pct(window) if variant in ("atr", "combined") else POSITION_SIZE_PCT
        pos_value = balance * pos_pct
        fee = pos_value * FEE_PCT

        if sig == Signal.BUY:
            volume = pos_value / price
            balance -= pos_value + fee
            position = {
                "entry": price, "volume": volume, "direction": "long",
                "sl": price * (1 - STOP_LOSS_PCT),
                "tp": price * (1 + TAKE_PROFIT_PCT),
                "margin": pos_value,
            }
        else:
            margin = pos_value * 0.20
            volume = pos_value / price
            balance -= margin + fee
            position = {
                "entry": price, "volume": volume, "direction": "short",
                "sl": price * (1 + STOP_LOSS_PCT),
                "tp": price * (1 - TAKE_PROFIT_PCT),
                "margin": margin,
            }

    # Letzte Position schließen
    if position:
        p = df["close"].iloc[-1]
        fee = p * position["volume"] * FEE_PCT
        d = position["direction"]
        if d == "long":
            pnl = (p - position["entry"]) * position["volume"] - 2 * fee
            balance += p * position["volume"] - fee
        else:
            pnl = (position["entry"] - p) * position["volume"] - 2 * fee
            balance += position["margin"] + pnl
        trades.append({"pnl": pnl, "exit": "end"})

    winners = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(winners) / len(trades) * 100 if trades else 0
    sl_hits = sum(1 for t in trades if t["exit"] == "sl")
    tp_hits = sum(1 for t in trades if t["exit"] == "tp")
    return {
        "pnl": round(total_pnl, 2),
        "trades": len(trades),
        "win_rate": round(win_rate, 1),
        "sl": sl_hits,
        "tp": tp_hits,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    variants = [
        ("baseline",    "BB-Filter (aktuell)"),
        ("multiframe",  "+ Multi-Timeframe 1h"),
        ("vwap",        "+ VWAP-Filter"),
        ("candle",      "+ Kerzenmuster"),
        ("atr",         "+ ATR Position Sizing"),
        ("consec",      "+ Konsekutive Verluste"),
        ("timefilter",  "+ Zeit-Filter (0-7 Uhr)"),
        ("combined",    "ALLE Filter kombiniert"),
    ]

    print("\n  Lade Daten von Binance...")
    datasets = {}
    for symbol in SYMBOLS:
        print(f"    {symbol}...", end=" ", flush=True)
        df = fetch_binance(symbol, months=MONTHS)
        if not df.empty and len(df) > WARMUP + 10:
            datasets[symbol] = df
            print(f"{len(df)} Kerzen")
        else:
            print("übersprungen")
        time.sleep(1.0)

    print(f"\n  Teste {len(variants)} Varianten auf {len(datasets)} Coins...\n")

    results = {}
    for variant_key, variant_name in variants:
        total_pnl = 0
        total_trades = 0
        total_sl = 0
        total_tp = 0
        win_trades = 0
        for symbol, df in datasets.items():
            r = run_variant(df, symbol, variant=variant_key)
            total_pnl += r["pnl"]
            total_trades += r["trades"]
            total_sl += r["sl"]
            total_tp += r["tp"]
            win_trades += round(r["trades"] * r["win_rate"] / 100)
        overall_win_rate = win_trades / total_trades * 100 if total_trades > 0 else 0
        results[variant_key] = {
            "name": variant_name,
            "pnl": round(total_pnl, 2),
            "trades": total_trades,
            "win_rate": round(overall_win_rate, 1),
            "sl": total_sl,
            "tp": total_tp,
        }
        baseline_pnl = results.get("baseline", {}).get("pnl", 0)
        diff = total_pnl - baseline_pnl if variant_key != "baseline" else 0
        diff_str = f"  ({diff:+.0f}EUR)" if variant_key != "baseline" else ""
        print(f"  {variant_name:<30}  P&L: {total_pnl:>+7.2f}EUR{diff_str:<12}  "
              f"Trades: {total_trades:>3}  Win: {overall_win_rate:>4.0f}%  "
              f"TP: {total_tp}  SL: {total_sl}")

    # Ranking
    print(f"\n  {'='*70}")
    print(f"  RANKING (nach P&L, Basis: BB-Filter = {results['baseline']['pnl']:+.2f}EUR)")
    print(f"  {'='*70}")
    ranked = sorted(results.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for i, (key, r) in enumerate(ranked, 1):
        diff = r["pnl"] - results["baseline"]["pnl"]
        marker = " ◄ BEST" if i == 1 else (" ◄ aktuell" if key == "baseline" else "")
        print(f"  {i}. {r['name']:<30}  {r['pnl']:>+7.2f}EUR  ({diff:>+5.0f}EUR){marker}")
    print()


if __name__ == "__main__":
    main()
