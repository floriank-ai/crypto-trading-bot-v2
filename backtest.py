#!/usr/bin/env python3
"""
Backtest: Testet die Momentum-Strategie gegen historische Daten.
Nutzt exakt denselben Strategy-Code wie der Live-Bot.

Datenquellen:
  --source kraken       letzte 7 Tage (Standard)
  --source cryptocompare  bis zu 3 Monate historische Daten (kostenlos, kein API Key)

Usage:
  python backtest.py
  python backtest.py --source cryptocompare --months 3
"""

import argparse
import ccxt
import requests
import pandas as pd
import time
from datetime import datetime, timedelta
from config import Config
from strategies import MomentumStrategy, Signal
import ta as ta_lib

# ── Konfiguration ──────────────────────────────────────────────────────────
SYMBOLS = [
    "BTC/EUR", "ETH/EUR", "SOL/EUR", "ADA/EUR",
    "XRP/EUR", "AVAX/EUR", "LINK/EUR", "DOT/EUR",
]
INITIAL_CAPITAL = 1000.0
POSITION_SIZE_PCT = 0.20   # 20% des Kapitals pro Trade
STOP_LOSS_PCT     = Config.STOP_LOSS_PCT
TAKE_PROFIT_PCT   = Config.TAKE_PROFIT_PCT
FEE_PCT           = 0.0026
TIMEFRAME         = "15m"
CANDLE_LIMIT      = 700    # ~1 Woche bei 15min-Kerzen
WARMUP_CANDLES    = 50     # Mindest-Kerzen für Indikatoren


# ── Daten laden ────────────────────────────────────────────────────────────

def fetch_data_kraken(exchange, symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=CANDLE_LIMIT)
        if not ohlcv:
            return pd.DataFrame()
        df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        return df
    except Exception as e:
        print(f"    Fehler beim Laden von {symbol}: {e}")
        return pd.DataFrame()


def fetch_data_binance(symbol, months=3):
    """
    Lädt bis zu 3 Monate 15min-Daten von Binance (kostenlos, kein API Key).
    Nutzt USDT-Paare (BTC/USDT statt BTC/EUR) — Preismuster identisch.
    Paginiert automatisch in 1000er-Blöcken.
    """
    base = symbol.split("/")[0]
    binance_symbol = f"{base}/USDT"

    total_candles = months * 30 * 24 * 4  # 15min-Kerzen
    all_rows = []

    try:
        binance = ccxt.binance({"enableRateLimit": True})
        binance.load_markets()
        if binance_symbol not in binance.symbols:
            print(f"    {binance_symbol} nicht auf Binance verfügbar")
            return pd.DataFrame()

        since_ms = int((datetime.utcnow() - timedelta(days=months * 30)).timestamp() * 1000)

        while len(all_rows) < total_candles:
            batch = binance.fetch_ohlcv(binance_symbol, "15m", since=since_ms, limit=1000)
            if not batch:
                break
            all_rows.extend(batch)
            last_ts = batch[-1][0]
            since_ms = last_ts + 1
            if len(batch) < 1000:
                break
            time.sleep(0.2)

    except Exception as e:
        print(f"    Binance Fehler: {e}")
        return pd.DataFrame()

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df = df[df["close"] > 0].reset_index(drop=True)
    return df


# ── Backtest-Engine ────────────────────────────────────────────────────────

class StrictMomentumStrategy:
    """Nur die stärksten Signale: Breakout/Breakdown + RSI-Extremwerte + ADX-Filter."""

    def analyze(self, df: pd.DataFrame) -> dict:
        if df.empty or len(df) < 30:
            return {"signal": Signal.HOLD, "reason": "Not enough data"}

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        rsi = ta_lib.momentum.RSIIndicator(close, window=14).rsi()
        current_rsi = rsi.iloc[-1]

        ema_f = ta_lib.trend.EMAIndicator(close, window=9).ema_indicator()
        ema_s = ta_lib.trend.EMAIndicator(close, window=21).ema_indicator()
        bullish = ema_f.iloc[-1] > ema_s.iloc[-1]
        bearish = ema_f.iloc[-1] < ema_s.iloc[-1]

        macd = ta_lib.trend.MACD(close)
        macd_hist = macd.macd_diff().iloc[-1]

        # ADX: nur bei echtem Trend handeln (ADX > 20)
        adx = ta_lib.trend.ADXIndicator(high, low, close, window=14).adx()
        trending = adx.iloc[-1] > 20

        avg_vol = df["volume"].rolling(20).mean().iloc[-1]
        vol_spike = avg_vol > 0 and df["volume"].iloc[-1] > avg_vol * 1.8

        high_20 = close.rolling(20).max().iloc[-2] if len(df) >= 21 else 0
        low_20  = close.rolling(20).min().iloc[-2] if len(df) >= 21 else 999999
        breakout  = close.iloc[-1] > high_20 and vol_spike
        breakdown = close.iloc[-1] < low_20  and vol_spike

        signal = Signal.HOLD
        reasons = []
        leverage = 1

        # NUR starke Signale mit ADX-Bestätigung
        if trending:
            if breakout and bullish:
                signal = Signal.BUY
                reasons = ["Breakout new high + volume spike"]
                leverage = 2
            elif current_rsi < 32 and bullish and macd_hist > 0:
                signal = Signal.BUY
                reasons = [f"RSI {current_rsi:.0f} extreme oversold + MACD pos"]
                leverage = 2
            elif breakdown and bearish:
                signal = Signal.SELL
                reasons = ["Breakdown new low + volume spike"]
                leverage = 2
            elif current_rsi > 68 and bearish and macd_hist < 0:
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


class BBMomentumStrategy:
    """Strict strategy + Bollinger Band filter to avoid entering overdehnte moves."""

    def analyze(self, df: pd.DataFrame) -> dict:
        if df.empty or len(df) < 30:
            return {"signal": Signal.HOLD, "reason": "Not enough data"}

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        rsi = ta_lib.momentum.RSIIndicator(close, window=14).rsi()
        current_rsi = rsi.iloc[-1]

        ema_f = ta_lib.trend.EMAIndicator(close, window=9).ema_indicator()
        ema_s = ta_lib.trend.EMAIndicator(close, window=21).ema_indicator()
        bullish = ema_f.iloc[-1] > ema_s.iloc[-1]
        bearish = ema_f.iloc[-1] < ema_s.iloc[-1]

        macd = ta_lib.trend.MACD(close)
        macd_hist = macd.macd_diff().iloc[-1]

        adx = ta_lib.trend.ADXIndicator(high, low, close, window=14).adx()
        trending = adx.iloc[-1] > 20

        avg_vol = df["volume"].rolling(20).mean().iloc[-1]
        vol_spike = avg_vol > 0 and df["volume"].iloc[-1] > avg_vol * 1.8

        high_20 = close.rolling(20).max().iloc[-2] if len(df) >= 21 else 0
        low_20  = close.rolling(20).min().iloc[-2] if len(df) >= 21 else 999999
        breakout  = close.iloc[-1] > high_20 and vol_spike
        breakdown = close.iloc[-1] < low_20  and vol_spike

        # Bollinger Bands (20-period, 2 std)
        bb = ta_lib.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_upper  = bb.bollinger_hband().iloc[-1]
        bb_middle = bb.bollinger_mavg().iloc[-1]
        bb_lower  = bb.bollinger_lband().iloc[-1]
        price_now = close.iloc[-1]

        signal = Signal.HOLD
        reasons = []
        leverage = 1

        if trending:
            if breakout and bullish:
                # Block if price is already >2% above upper BB (too stretched)
                if price_now > bb_upper * 1.02:
                    pass  # overdehnt — false breakout risk
                else:
                    signal = Signal.BUY
                    reasons = ["Breakout new high + volume spike"]
                    leverage = 2

            elif current_rsi < 32 and bullish and macd_hist > 0:
                # RSI oversold: confirm price is near/below middle BB (actually cheap)
                if price_now > bb_middle:
                    pass  # RSI oversold but price still above midline — skip
                else:
                    signal = Signal.BUY
                    reasons = [f"RSI {current_rsi:.0f} extreme oversold + MACD pos"]
                    leverage = 2

            elif breakdown and bearish:
                # Block if price already >2% below lower BB (too stretched, bounce risk)
                if price_now < bb_lower * 0.98:
                    pass  # overdehnt nach unten
                else:
                    signal = Signal.SELL
                    reasons = ["Breakdown new low + volume spike"]
                    leverage = 2

            elif current_rsi > 68 and bearish and macd_hist < 0:
                # RSI overbought: confirm price is near/above middle BB
                if price_now < bb_middle:
                    pass  # RSI overbought but price already below midline — skip
                else:
                    signal = Signal.SELL
                    reasons = [f"RSI {current_rsi:.0f} extreme overbought + MACD neg"]
                    leverage = 2

        return {
            "signal": signal,
            "reason": " + ".join(reasons) if reasons else "No signal",
            "rsi": round(current_rsi, 2),
            "price": round(price_now, 2),
            "strategy": "momentum",
            "leverage": leverage,
        }


def run_backtest(df, symbol, time_limit_candles=None, btc_df=None, direction="both", strict=False, bb_filter=False):
    if bb_filter:
        strategy = BBMomentumStrategy()
    elif strict:
        strategy = StrictMomentumStrategy()
    else:
        strategy = MomentumStrategy()
    balance = INITIAL_CAPITAL
    position = None
    trades = []
    portfolio_curve = [INITIAL_CAPITAL]

    for i in range(WARMUP_CANDLES, len(df)):
        window = df.iloc[:i + 1]
        price  = df["close"].iloc[i]
        ts     = df["time"].iloc[i]

        # ── Exit prüfen ──────────────────────────────────────────────────
        if position:
            d = position["direction"]
            hit_sl = (d == "long"  and price <= position["sl"]) or \
                     (d == "short" and price >= position["sl"])
            hit_tp = (d == "long"  and price >= position["tp"]) or \
                     (d == "short" and price <= position["tp"])
            hit_time = time_limit_candles and (i - position["entry_candle"]) >= time_limit_candles

            if hit_sl or hit_tp or hit_time:
                exit_type = "stop_loss" if hit_sl else ("take_profit" if hit_tp else "time_exit")
                fee = price * position["volume"] * FEE_PCT

                if d == "long":
                    pnl = (price - position["entry"]) * position["volume"] - 2 * fee
                    balance += price * position["volume"] - fee
                else:
                    pnl = (position["entry"] - price) * position["volume"] - 2 * fee
                    balance += position["margin"] + pnl

                trades.append({
                    "time": ts, "symbol": symbol, "direction": d,
                    "entry": position["entry"], "exit_price": price,
                    "pnl": round(pnl, 4), "exit": exit_type,
                })
                position = None

        # ── BTC Trendfilter (24h = 96 Kerzen) ────────────────────────────
        btc_bullish = btc_bearish = False
        if btc_df is not None and i < len(btc_df):
            btc_now = btc_df["close"].iloc[min(i, len(btc_df)-1)]
            btc_24h = btc_df["close"].iloc[max(0, min(i, len(btc_df)-1) - 96)]
            btc_change = (btc_now - btc_24h) / btc_24h
            btc_bullish = btc_change > 0.005   # +0.5% über 24h
            btc_bearish = btc_change < -0.005  # -0.5% über 24h

        # ── Einstieg prüfen (nur wenn keine offene Position) ─────────────
        if position is None and balance > 10:
            sig = strategy.analyze(window)

            if sig["signal"] == Signal.BUY:
                if direction == "short":
                    pass  # nur Shorts erlaubt
                elif btc_df is not None and btc_bearish:
                    pass  # kein Long bei BTC-Abwärtstrend
                pos_value = balance * POSITION_SIZE_PCT
                fee = pos_value * FEE_PCT
                volume = pos_value / price
                balance -= pos_value + fee
                position = {
                    "entry": price, "volume": volume, "direction": "long",
                    "sl": price * (1 - STOP_LOSS_PCT),
                    "tp": price * (1 + TAKE_PROFIT_PCT),
                    "margin": pos_value, "entry_candle": i,
                }

            elif sig["signal"] == Signal.SELL:
                if direction == "long":
                    pass  # nur Longs erlaubt
                elif btc_df is not None and btc_bullish:
                    pass  # kein Short bei BTC-Aufwärtstrend
                else:
                    pos_value = balance * POSITION_SIZE_PCT
                    margin = pos_value * 0.20
                    fee = pos_value * FEE_PCT
                    volume = pos_value / price
                    balance -= margin + fee
                    position = {
                        "entry": price, "volume": volume, "direction": "short",
                        "sl": price * (1 + STOP_LOSS_PCT),
                        "tp": price * (1 - TAKE_PROFIT_PCT),
                        "margin": margin, "entry_candle": i,
                    }

        # ── Portfolio-Wert tracken ────────────────────────────────────────
        if position:
            d = position["direction"]
            if d == "long":
                port = balance + price * position["volume"]
            else:
                pnl_open = (position["entry"] - price) * position["volume"]
                port = balance + position["margin"] + pnl_open
        else:
            port = balance
        portfolio_curve.append(port)

    # ── Letzte Position am Ende schließen ────────────────────────────────
    if position:
        final_price = df["close"].iloc[-1]
        final_ts    = df["time"].iloc[-1]
        fee = final_price * position["volume"] * FEE_PCT
        d   = position["direction"]
        if d == "long":
            pnl = (final_price - position["entry"]) * position["volume"] - 2 * fee
            balance += final_price * position["volume"] - fee
        else:
            pnl = (position["entry"] - final_price) * position["volume"] - 2 * fee
            balance += position["margin"] + pnl
        trades.append({
            "time": final_ts, "symbol": symbol, "direction": d,
            "entry": position["entry"], "exit_price": final_price,
            "pnl": round(pnl, 4), "exit": "end",
        })

    return {"symbol": symbol, "final_balance": balance, "trades": trades, "curve": portfolio_curve}


# ── Ergebnisse ausgeben ────────────────────────────────────────────────────

def print_results(results):
    print("\n" + "=" * 70)
    print("  BACKTEST ERGEBNISSE  —  Momentum-Strategie  —  letzte 7 Tage")
    print("=" * 70)

    all_trades = []
    for r in results:
        all_trades.extend(r["trades"])

    for r in results:
        trades = r["trades"]
        if not trades:
            print(f"\n  {r['symbol']:15s}  —  keine Trades")
            continue

        winners = [t for t in trades if t["pnl"] > 0]
        losers  = [t for t in trades if t["pnl"] <= 0]
        win_rate   = len(winners) / len(trades) * 100
        total_pnl  = sum(t["pnl"] for t in trades)
        pnl_pct    = (r["final_balance"] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        # Max Drawdown
        curve = r["curve"]
        peak = curve[0]
        max_dd = 0.0
        for v in curve:
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        avg_win  = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t["pnl"] for t in losers)  / len(losers)  if losers  else 0

        print(f"\n  {r['symbol']}")
        print(f"    Trades: {len(trades):3d}  |  Win-Rate: {win_rate:4.0f}%  |  P&L: {total_pnl:+7.2f}EUR ({pnl_pct:+5.1f}%)")
        print(f"    Max Drawdown: {max_dd:4.1f}%  |  Ø Gewinn: {avg_win:+.2f}EUR  |  Ø Verlust: {avg_loss:+.2f}EUR")
        tp_count = sum(1 for t in trades if t["exit"] == "take_profit")
        sl_count = sum(1 for t in trades if t["exit"] == "stop_loss")
        print(f"    Take-Profits: {tp_count}  |  Stop-Losses: {sl_count}")

    if all_trades:
        total_pnl = sum(t["pnl"] for t in all_trades)
        win_rate  = len([t for t in all_trades if t["pnl"] > 0]) / len(all_trades) * 100
        tp_total  = sum(1 for t in all_trades if t["exit"] == "take_profit")
        sl_total  = sum(1 for t in all_trades if t["exit"] == "stop_loss")
        print(f"\n  {'─'*60}")
        print(f"  GESAMT: {len(all_trades)} Trades  |  Win-Rate: {win_rate:.0f}%  |  P&L: {total_pnl:+.2f}EUR")
        print(f"  Take-Profits: {tp_total}  |  Stop-Losses: {sl_total}")
        print(f"  {'─'*60}\n")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["kraken", "binance"], default="kraken",
                        help="Datenquelle (default: kraken = letzte 7 Tage, binance = bis 3 Monate)")
    parser.add_argument("--months", type=int, default=3,
                        help="Monate historische Daten bei Binance (default: 3)")
    parser.add_argument("--tp", type=float, default=None,
                        help="Take-Profit in Prozent ueberschreiben z.B. 12 fuer 12 Prozent")
    parser.add_argument("--time-limit", type=int, default=None,
                        help="Zeitlimit in Stunden fuer Positionen (z.B. 4)")
    parser.add_argument("--btc-filter", action="store_true",
                        help="24h BTC Trendfilter: nur Longs bei bullish, nur Shorts bei bearish")
    parser.add_argument("--direction", choices=["long", "short", "both"], default="both",
                        help="Nur Long, nur Short, oder beide Richtungen testen")
    parser.add_argument("--strict", action="store_true",
                        help="Strenge Strategie: nur starke Signale mit ADX-Filter")
    parser.add_argument("--bb-filter", action="store_true",
                        help="Bollinger Band Filter: blockiert overdehnte Einstiege")
    args = parser.parse_args()

    global TAKE_PROFIT_PCT
    if args.tp is not None:
        TAKE_PROFIT_PCT = args.tp / 100.0

    source_label = f"Binance — letzte {args.months} Monate (USDT-Paare)" if args.source == "binance" \
                   else "Kraken — letzte 7 Tage"

    print("\n" + "=" * 70)
    print(f"  CRYPTO BACKTEST — {source_label} (15min Kerzen)")
    print(f"  SL: {STOP_LOSS_PCT*100:.0f}%  |  TP: {TAKE_PROFIT_PCT*100:.0f}%  |  Position: {POSITION_SIZE_PCT*100:.0f}%  |  Fee: {FEE_PCT*100:.2f}%")
    print("=" * 70)

    exchange = None
    if args.source == "kraken":
        exchange = ccxt.kraken({"enableRateLimit": True})
        exchange.load_markets()

    # BTC-Daten für Trendfilter laden
    btc_df = None
    if args.btc_filter and args.source == "binance":
        print("\n  Lade BTC/USDT für Trendfilter...", end=" ", flush=True)
        btc_df = fetch_data_binance("BTC/EUR", months=args.months)
        print(f"{len(btc_df)} Kerzen")

    results = []
    for symbol in SYMBOLS:
        print(f"\n  Lade {symbol}...", end=" ", flush=True)

        if args.source == "binance":
            df = fetch_data_binance(symbol, months=args.months)
        else:
            df = fetch_data_kraken(exchange, symbol)

        if df.empty or len(df) < WARMUP_CANDLES + 10:
            print("nicht genug Daten")
            continue

        period = f"{df['time'].iloc[0].strftime('%d.%m.%y %H:%M')} → {df['time'].iloc[-1].strftime('%d.%m.%y %H:%M')}"
        print(f"{len(df)} Kerzen  [{period}]")
        time_limit_candles = args.time_limit * 4 if args.time_limit else None
        result = run_backtest(df, symbol, time_limit_candles=time_limit_candles, btc_df=btc_df, direction=args.direction, strict=args.strict, bb_filter=args.bb_filter)
        results.append(result)

    print_results(results)


if __name__ == "__main__":
    main()
