"""
Auto-Optimizer: Läuft jeden Sonntag, testet alle Coins mit der Momentum-Strategie
gegen die letzten 3 Monate und aktualisiert die MOMENTUM_SKIP-Liste automatisch.

Logik:
  - P&L < -10 EUR  → Coin wird zur Skip-Liste hinzugefügt
  - P&L > +10 EUR  → Coin wird von der Skip-Liste entfernt
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

SKIP_THRESHOLD  = -10.0   # EUR — schlechter als das → überspringen
KEEP_THRESHOLD  =  10.0   # EUR — besser als das → wieder aufnehmen
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
    return {"last_run": None, "skip_list": list(Config.MOMENTUM_SKIP)}


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
    Führt den Backtest durch, aktualisiert die Skip-Liste und
    gibt die neue Skip-Liste zurück. Schickt Telegram-Bericht.
    """
    print("\n  [Optimizer] Starte wöchentlichen Backtest aller Kraken-Coins...")
    state = load_state()
    current_skip = set(state.get("skip_list", list(Config.MOMENTUM_SKIP)))
    results = {}

    symbols = get_testable_symbols()

    # Binance einmal laden und wiederverwenden (spart Zeit)
    binance = ccxt.binance({"enableRateLimit": True})
    binance.load_markets()

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

    # Skip-Liste aktualisieren
    added, removed = [], []
    for sym, pnl in results.items():
        if pnl < SKIP_THRESHOLD and sym not in current_skip:
            current_skip.add(sym)
            added.append(f"{sym} ({pnl:+.0f}EUR)")
        elif pnl > KEEP_THRESHOLD and sym in current_skip:
            current_skip.discard(sym)
            removed.append(f"{sym} ({pnl:+.0f}EUR)")

    new_skip = sorted(current_skip)

    # Zustand speichern
    state["last_run"] = datetime.now().isoformat()
    state["skip_list"] = new_skip
    state["last_results"] = results
    save_state(state)

    # Ranking ausgeben
    ranked = sorted(results.items(), key=lambda x: x[1], reverse=True)
    print("\n  [Optimizer] Ergebnisse (3 Monate):")
    for sym, pnl in ranked:
        status = "⛔ SKIP" if sym in current_skip else "✅ AKTIV"
        print(f"    {sym:15s} {pnl:+7.2f}EUR  {status}")
    print(f"  [Optimizer] Skip-Liste: {new_skip}")

    # Telegram-Bericht (nur Top 5, Bottom 5 und Änderungen)
    if notifier:
        lines = [f"📊 *Wöchentlicher Backtest* ({len(results)} Coins, 3 Monate)\n"]
        lines.append("🏆 *Top 5:*")
        for sym, pnl in ranked[:5]:
            lines.append(f"  ✅ {sym}: `{pnl:+.0f}EUR`")
        lines.append("\n💀 *Schlechteste 5:*")
        for sym, pnl in ranked[-5:]:
            lines.append(f"  ⛔ {sym}: `{pnl:+.0f}EUR`")
        if added:
            lines.append(f"\n🆕 Neu Skip: {', '.join(added)}")
        if removed:
            lines.append(f"\n♻️ Wieder aktiv: {', '.join(removed)}")
        if not added and not removed:
            lines.append("\n_Keine Änderungen._")
        lines.append(f"\nSkip-Liste: {len(new_skip)} Coins")
        notifier.send("\n".join(lines))

    return new_skip
