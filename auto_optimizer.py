"""
Auto-Optimizer: Läuft jeden Sonntag, testet alle Coins mit der Momentum-Strategie
gegen die letzten 3 Monate und erstellt eine Prioritätsliste der besten Coins.

Logik:
  - Alle Kraken EUR-Coins werden backtested
  - Top 20 nach P&L → Prioritätsliste (werden zuerst gescannt)
  - Kein Ban mehr — alle Coins bleiben handelbar, Strategie-Filter entscheiden
  - Ergebnis wird per Telegram gesendet
  - Zustand wird in logs/optimizer_state.json gespeichert (Railway-persistent)
"""

import json
import os
import time
import ccxt
import pandas as pd
from datetime import datetime, timedelta
from config import Config
from strategies import MomentumStrategy, Signal


STATE_PATH = os.path.join("logs", "optimizer_state.json")

PRIORITY_COUNT  = 20      # Top N Coins in die Prioritätsliste
MONTHS          = 3
POSITION_PCT    = 0.20
FEE_PCT         = 0.0026
WARMUP          = 50
MIN_CANDLES     = 500     # Mindest-Kerzen für aussagekräftigen Backtest (~5 Tage)


def get_testable_symbols() -> list[str]:
    """
    Gibt alle Kraken EUR-Paare zurück die auch auf Binance als USDT-Paar verfügbar sind.
    So werden alle vom Scanner gefundenen Coins berücksichtigt.
    """
    try:
        kraken = ccxt.kraken({"enableRateLimit": True})
        kraken.load_markets()
        kraken_eur = [s for s in kraken.symbols if s.endswith("/EUR")
                      and kraken.markets[s].get("active", True)]

        binance = ccxt.binance({"enableRateLimit": True})
        binance.load_markets()
        binance_usdt = set(s.split("/")[0] for s in binance.symbols if s.endswith("/USDT"))

        testable = [s for s in kraken_eur if s.split("/")[0] in binance_usdt]
        print(f"  [Optimizer] {len(kraken_eur)} Kraken EUR-Paare → {len(testable)} auf Binance testbar")
        return sorted(testable)
    except Exception as e:
        print(f"  [Optimizer] Fehler beim Laden der Symbole: {e}")
        # Fallback auf bekannte Coins
        return ["BTC/EUR", "ETH/EUR", "SOL/EUR", "ADA/EUR", "XRP/EUR",
                "AVAX/EUR", "LINK/EUR", "DOT/EUR", "BNB/EUR", "ATOM/EUR"]


# ── Persistenz ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_run": None, "priority_list": []}


def save_state(state: dict):
    os.makedirs("logs", exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def should_run_today() -> bool:
    """Gibt True zurück wenn heute Sonntag und letzte Ausführung >6 Tage her."""
    if datetime.now().weekday() != 6:  # 6 = Sonntag
        return False
    state = load_state()
    last = state.get("last_run")
    if not last:
        return True
    last_dt = datetime.fromisoformat(last)
    return (datetime.now() - last_dt).days >= 6


# ── Daten & Backtest ────────────────────────────────────────────────────────

def _fetch_binance(symbol: str, months: int, binance_exchange=None) -> pd.DataFrame:
    base = symbol.split("/")[0]
    binance_sym = f"{base}/USDT"
    try:
        b = binance_exchange or ccxt.binance({"enableRateLimit": True})
        if not binance_exchange:
            b.load_markets()
        if binance_sym not in b.symbols:
            return pd.DataFrame()
        since = int((datetime.utcnow() - timedelta(days=months * 30)).timestamp() * 1000)
        rows = []
        while True:
            batch = b.fetch_ohlcv(binance_sym, "15m", since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            since = batch[-1][0] + 1
            if len(batch) < 1000:
                break
            time.sleep(0.2)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        return df[df["close"] > 0].reset_index(drop=True)
    except Exception as e:
        print(f"    [Optimizer] Fehler beim Laden von {symbol}: {e}")
        return pd.DataFrame()


def _backtest_coin(df: pd.DataFrame) -> float:
    """Gibt den realisierten P&L (EUR) der Momentum-Strategie zurück."""
    strat = MomentumStrategy()
    balance = 1000.0
    position = None
    sl = Config.STOP_LOSS_PCT
    tp = Config.TAKE_PROFIT_PCT

    for i in range(WARMUP, len(df)):
        window = df.iloc[:i + 1]
        price = df["close"].iloc[i]

        if position:
            d = position["direction"]
            hit_sl = (d == "long"  and price <= position["sl"]) or \
                     (d == "short" and price >= position["sl"])
            hit_tp = (d == "long"  and price >= position["tp"]) or \
                     (d == "short" and price <= position["tp"])
            if hit_sl or hit_tp:
                fee = price * position["volume"] * FEE_PCT
                if d == "long":
                    balance += price * position["volume"] - fee
                else:
                    pnl = (position["entry"] - price) * position["volume"] - 2 * fee
                    balance += position["margin"] + pnl
                position = None

        if position is None and balance > 10:
            sig = strat.analyze(window)
            if sig["signal"] == Signal.BUY:
                pv = balance * POSITION_PCT
                balance -= pv + pv * FEE_PCT
                position = {"entry": price, "volume": pv / price, "direction": "long",
                            "sl": price * (1 - sl), "tp": price * (1 + tp), "margin": pv}
            elif sig["signal"] == Signal.SELL:
                pv = balance * POSITION_PCT
                margin = pv * 0.20
                balance -= margin + pv * FEE_PCT
                position = {"entry": price, "volume": pv / price, "direction": "short",
                            "sl": price * (1 + sl), "tp": price * (1 - tp), "margin": margin}

    # Letzte Position schließen
    if position:
        p = df["close"].iloc[-1]
        fee = p * position["volume"] * FEE_PCT
        if position["direction"] == "long":
            balance += p * position["volume"] - fee
        else:
            pnl = (position["entry"] - p) * position["volume"] - 2 * fee
            balance += position["margin"] + pnl

    return round(balance - 1000.0, 2)


# ── Haupt-Funktion ──────────────────────────────────────────────────────────

def run(notifier=None) -> list[str]:
    """
    Führt den Backtest durch, erstellt eine Prioritätsliste der besten Coins
    und gibt diese zurück. Kein Ban — alle Coins bleiben handelbar.
    """
    print("\n  [Optimizer] Starte wöchentlichen Backtest aller Kraken-Coins...")
    state = load_state()
    results = {}

    symbols = get_testable_symbols()

    # Binance einmal laden und wiederverwenden (spart Zeit).
    # Railway-IP ist bei Binance oft geblockt (HTTP 451) → sofort sauber abbrechen
    # und State speichern, damit nicht jeder Cycle den Optimizer neu triggert.
    binance = ccxt.binance({"enableRateLimit": True})
    try:
        binance.load_markets()
    except Exception as e:
        print(f"  [Optimizer] Binance nicht erreichbar (Geo-Block?): {e}")
        # State mit last_run=jetzt speichern damit erst nächste Woche wieder versucht wird
        state["last_run"] = datetime.now().isoformat()
        state["last_error"] = str(e)[:200]
        save_state(state)
        if notifier:
            notifier.send(f"⚠️ *Optimizer übersprungen*\nBinance blockt Railway-IP (451).\nNächster Versuch in 7 Tagen.")
        return state.get("priority_list", [])

    for symbol in symbols:
        print(f"  [Optimizer] {symbol}...", end=" ", flush=True)
        df = _fetch_binance(symbol, MONTHS, binance_exchange=binance)
        if df.empty or len(df) < MIN_CANDLES:
            print("übersprungen (zu wenig Daten)")
            continue
        pnl = _backtest_coin(df)
        results[symbol] = pnl
        print(f"{pnl:+.2f}EUR")
        time.sleep(0.15)

    # Prioritätsliste: Top N Coins nach P&L (nur profitable)
    ranked = sorted(results.items(), key=lambda x: x[1], reverse=True)
    priority_list = [sym for sym, pnl in ranked[:PRIORITY_COUNT] if pnl > 0]

    # Zustand speichern
    state["last_run"] = datetime.now().isoformat()
    state["priority_list"] = priority_list
    state["last_results"] = results
    save_state(state)

    # Ranking ausgeben
    print("\n  [Optimizer] Ergebnisse (3 Monate):")
    for sym, pnl in ranked:
        tag = "⭐ PRIO" if sym in priority_list else "   "
        print(f"    {sym:15s} {pnl:+7.2f}EUR  {tag}")
    print(f"  [Optimizer] Prioritätsliste ({len(priority_list)}): {priority_list}")

    # Telegram-Bericht
    if notifier:
        lines = [f"📊 *Wöchentlicher Backtest* ({len(results)} Coins, 3 Monate)\n"]
        lines.append(f"⭐ *Top {PRIORITY_COUNT} Priorität:*")
        for sym in priority_list[:10]:
            pnl = results.get(sym, 0)
            lines.append(f"  {sym}: `{pnl:+.0f}EUR`")
        lines.append("\n💀 *Schlechteste 5:*")
        for sym, pnl in ranked[-5:]:
            lines.append(f"  {sym}: `{pnl:+.0f}EUR`")
        lines.append(f"\n_Kein Ban — alle Coins handelbar. {len(priority_list)} Coins bevorzugt._")
        notifier.send("\n".join(lines))

    return priority_list
