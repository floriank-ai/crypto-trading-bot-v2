#!/usr/bin/env python3
"""
Backtest: Testet die Momentum-Strategie gegen historische Kraken-Daten.
Nutzt exakt denselben Strategy-Code wie der Live-Bot.

Usage: python backtest.py
"""

import ccxt
import pandas as pd
import time
from datetime import datetime
from config import Config
from strategies import MomentumStrategy, Signal

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

def fetch_data(exchange, symbol):
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


# ── Backtest-Engine ────────────────────────────────────────────────────────

def run_backtest(df, symbol):
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

            if hit_sl or hit_tp:
                exit_type = "stop_loss" if hit_sl else "take_profit"
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

        # ── Einstieg prüfen (nur wenn keine offene Position) ─────────────
        if position is None and balance > 10:
            sig = strategy.analyze(window)

            if sig["signal"] == Signal.BUY:
                pos_value = balance * POSITION_SIZE_PCT
                fee = pos_value * FEE_PCT
                volume = pos_value / price
                balance -= pos_value + fee
                position = {
                    "entry": price, "volume": volume, "direction": "long",
                    "sl": price * (1 - STOP_LOSS_PCT),
                    "tp": price * (1 + TAKE_PROFIT_PCT),
                    "margin": pos_value,
                }

            elif sig["signal"] == Signal.SELL:
                pos_value = balance * POSITION_SIZE_PCT
                margin = pos_value * 0.20
                fee = pos_value * FEE_PCT
                volume = pos_value / price
                balance -= margin + fee
                position = {
                    "entry": price, "volume": volume, "direction": "short",
                    "sl": price * (1 + STOP_LOSS_PCT),
                    "tp": price * (1 - TAKE_PROFIT_PCT),
                    "margin": margin,
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
    print("\n" + "=" * 70)
    print("  CRYPTO BACKTEST — letzte 7 Tage (15min Kerzen, Kraken)")
    print(f"  SL: {STOP_LOSS_PCT*100:.0f}%  |  TP: {TAKE_PROFIT_PCT*100:.0f}%  |  Position: {POSITION_SIZE_PCT*100:.0f}%  |  Fee: {FEE_PCT*100:.2f}%")
    print("=" * 70)

    exchange = ccxt.kraken({"enableRateLimit": True})
    exchange.load_markets()

    results = []
    for symbol in SYMBOLS:
        print(f"\n  Lade {symbol}...", end=" ", flush=True)
        df = fetch_data(exchange, symbol)
        if df.empty or len(df) < WARMUP_CANDLES + 10:
            print("nicht genug Daten")
            continue
        period = f"{df['time'].iloc[0].strftime('%d.%m %H:%M')} → {df['time'].iloc[-1].strftime('%d.%m %H:%M')}"
        print(f"{len(df)} Kerzen  [{period}]")
        result = run_backtest(df, symbol)
        results.append(result)
        time.sleep(0.3)

    print_results(results)


if __name__ == "__main__":
    main()
