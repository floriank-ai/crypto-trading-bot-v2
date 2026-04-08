#!/usr/bin/env python3
"""
Portfolio Status — liest direkt aus dem Bot-Zustand.
Aufruf: python portfolio_status.py
"""
from exchange import Exchange
from risk_manager import RiskManager
from config import Config
from trade_logger import TradeLogger
import json
from datetime import datetime

def main():
    exchange = Exchange()
    risk_mgr = RiskManager()
    logger = TradeLogger()

    # Trades laden — nur aktuelle Session (ab letztem session_start Marker)
    try:
        with open("logs/trades.json") as f:
            all_entries = json.load(f)
    except Exception:
        all_entries = []

    # Finde letzten session_start
    session_start_idx = 0
    session_capital = Config.INITIAL_CAPITAL
    for i, e in enumerate(all_entries):
        if e.get("session_start"):
            session_start_idx = i + 1
            session_capital = e.get("initial_capital", Config.INITIAL_CAPITAL)

    trades = [e for e in all_entries[session_start_idx:] if not e.get("session_start")]

    if not trades:
        print("Noch keine Trades seit dem letzten Start.")
        return

    start = session_capital

    # Positionen aus Trades rekonstruieren (sauber — eine Position pro Symbol)
    positions = {}  # symbol -> {volume, cost, direction, entry_price, strategy}

    for t in trades:
        sym = t["pair"]
        side = t["side"]
        vol = t["volume"]
        price = t["price_eur"]
        cost = t["total_eur"]

        if side == "buy":
            if sym not in positions:
                positions[sym] = {"volume": 0, "cost": 0, "direction": "long",
                                  "entry_price": price, "strategy": t.get("strategy", "")}
            positions[sym]["volume"] += vol
            positions[sym]["cost"] += cost

        elif side == "short":
            if sym not in positions:
                positions[sym] = {"volume": 0, "cost": 0, "direction": "short",
                                  "entry_price": price, "strategy": t.get("strategy", "")}
            positions[sym]["volume"] += vol
            positions[sym]["cost"] += cost * 0.20  # nur Margin

        elif side == "sell":
            if sym in positions:
                positions[sym]["volume"] -= vol
                if positions[sym]["volume"] <= 0.0001:
                    del positions[sym]

    # Aktuelle Preise und P&L
    cash = trades[-1]["balance_after"]
    total_pos_value = 0
    rows = []

    for sym, pos in positions.items():
        if pos["volume"] < 0.0001:
            continue
        ticker = exchange.get_ticker(sym)
        if not ticker or not ticker.get("last"):
            continue
        price = ticker["last"]

        if pos["direction"] == "short":
            pnl = (pos["entry_price"] - price) * pos["volume"]
            val = pos["cost"] + pnl
        else:
            val = pos["volume"] * price
            pnl = val - pos["cost"]

        total_pos_value += val
        pct = (pnl / pos["cost"] * 100) if pos["cost"] > 0 else 0
        rows.append((sym, pos["direction"], pos["entry_price"], price, pnl, pct))

    portfolio = cash + total_pos_value
    pnl_total = portfolio - start
    pnl_pct = (pnl_total / start) * 100

    # Ausgabe
    buys  = len([t for t in trades if t["side"] == "buy"])
    sells = len([t for t in trades if t["side"] == "sell"])
    shorts = len([t for t in trades if t["side"] == "short"])
    start_time = trades[0]["timestamp"][11:16]
    end_time   = trades[-1]["timestamp"][11:16]

    print(f"\n{'='*55}")
    print(f"  PORTFOLIO STATUS")
    print(f"  Zeitraum: {start_time} - {end_time}")
    print(f"  Trades:   {buys} Long | {shorts} Short | {sells} Exits")
    print(f"{'='*55}")
    print(f"  Cash:        {cash:>10.2f} EUR")
    print(f"  Positionen:  {total_pos_value:>10.2f} EUR  ({len(rows)} offen)")
    print(f"  PORTFOLIO:   {portfolio:>10.2f} EUR")
    print(f"  START:       {start:>10.2f} EUR")
    print(f"  P&L:         {pnl_total:>+10.2f} EUR  ({pnl_pct:+.2f}%)")
    print(f"{'='*55}")

    if rows:
        print(f"\n  {'Coin':<14} {'Dir':>5} {'Entry':>9} {'Jetzt':>9} {'P&L':>8} {'%':>7}")
        print(f"  {'-'*55}")
        for sym, d, entry, price, pnl, pct in sorted(rows, key=lambda x: x[4], reverse=True):
            arrow = "▲" if pnl >= 0 else "▼"
            print(f"  {sym:<14} {d:>5} {entry:>9.4f} {price:>9.4f} {pnl:>+7.2f}E {pct:>+6.1f}% {arrow}")

    print()

if __name__ == "__main__":
    main()
