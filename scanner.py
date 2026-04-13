import pandas as pd
import ta
from exchange import Exchange
from config import Config


class CoinScanner:
    """Scans all available coins and picks the best opportunities."""

    CORE_COINS = []  # keine erzwungenen Core-Coins — Scanner entscheidet nach Score

    # High volatility altcoins — prioritize these for 5%+ daily moves
    VOLATILE_COINS = [
        "PEPE/EUR", "DOGE/EUR", "SHIB/EUR",   # Meme coins — extreme moves
        "WIF/EUR", "BONK/EUR", "FLOKI/EUR",    # Newer memes
        "AVAX/EUR", "MATIC/EUR", "LINK/EUR",   # Mid caps
        "INJ/EUR", "TIA/EUR", "SEI/EUR",       # Trending L1s
        "SUI/EUR", "APT/EUR", "ARB/EUR",       # New L1/L2
    ]

    # Coins to skip (stablecoins, wrapped, low-liquidity, blacklist)
    SKIP_COINS = [
        "USDT/EUR", "USDC/EUR", "DAI/EUR", "PYUSD/EUR", "EURT/EUR",
        "BLUAI/EUR", "2Z/EUR", "ACU/EUR", "ADI/EUR", "ARC/EUR",  # illiquid/risky
        "AEVO/EUR", "ASTR/EUR", "ATH/EUR", "AKT/EUR",             # schlechte Performance
    ]

    def __init__(self, exchange: Exchange):
        self.exchange = exchange

    def scan(self) -> list[dict]:
        """Scan all EUR pairs and rank by opportunity score."""
        print(f"\n  Scanning top coins on Kraken...")

        all_pairs = self.exchange.get_all_eur_pairs()
        candidates = [p for p in all_pairs if p not in self.SKIP_COINS]

        # Put volatile coins first so they always get scanned
        volatile_available = [c for c in self.VOLATILE_COINS if c in candidates]
        rest = [c for c in candidates if c not in volatile_available]
        candidates = volatile_available + rest

        # Get bulk tickers
        tickers = self.exchange.get_tickers_bulk(candidates[:Config.SCAN_TOP_N])

        scored = []
        for symbol, ticker in tickers.items():
            if not ticker.get("last") or ticker["last"] == 0:
                continue
            if not ticker.get("volume") or ticker["volume"] < 50000:
                continue  # Skip low liquidity coins

            score = self._calculate_score(symbol, ticker)
            if score is not None:
                scored.append(score)

        # Sort by opportunity score
        scored.sort(key=lambda x: x["score"], reverse=True)

        # Always include core coins if not already in top picks
        top = scored[:Config.AUTO_PICK_COUNT]
        top_symbols = {s["symbol"] for s in top}

        for core in self.CORE_COINS:
            if core not in top_symbols:
                core_data = next((s for s in scored if s["symbol"] == core), None)
                if core_data:
                    top.append(core_data)

        return top

    def _calculate_score(self, symbol: str, ticker: dict) -> dict | None:
        """Calculate opportunity score for a coin."""
        try:
            score = 0.0
            reasons = []

            # Volume score (higher = better liquidity)
            vol = ticker.get("volume", 0)
            if vol > 100000:
                score += 3
                reasons.append("high volume")
            elif vol > 20000:
                score += 2
                reasons.append("good volume")
            else:
                score += 1

            # Price change momentum
            change = ticker.get("change_pct", 0) or 0
            if abs(change) > 5:
                score += 4
                reasons.append(f"big move {change:+.1f}%")
            elif abs(change) > 2:
                score += 2
                reasons.append(f"moving {change:+.1f}%")

            # Quick RSI check
            df = self.exchange.get_ohlcv(symbol, "15m", limit=30)
            if not df.empty and len(df) >= 14:
                rsi = ta.momentum.RSIIndicator(df["close"], window=14).rsi().iloc[-1]
                if rsi < 30:
                    score += 5
                    reasons.append(f"RSI oversold ({rsi:.0f})")
                elif rsi < 40:
                    score += 3
                    reasons.append(f"RSI low ({rsi:.0f})")
                elif rsi > 70:
                    score += 3
                    reasons.append(f"RSI overbought ({rsi:.0f})")

                    # Volume spike + breakout detection
                if len(df) >= 20:
                    avg_vol = df["volume"].rolling(20).mean().iloc[-1]
                    current_vol = df["volume"].iloc[-1]
                    high_20 = df["close"].rolling(20).max().iloc[-2]
                    current_close = df["close"].iloc[-1]

                    if avg_vol > 0 and current_vol > avg_vol * 2.5:
                        score += 6
                        reasons.append("massive volume spike!")
                    elif avg_vol > 0 and current_vol > avg_vol * 2:
                        score += 4
                        reasons.append("volume spike!")
                    elif avg_vol > 0 and current_vol > avg_vol * 1.5:
                        score += 2
                        reasons.append("volume rising")

                    # Breakout bonus
                    if current_close > high_20 and avg_vol > 0 and current_vol > avg_vol * 1.5:
                        score += 5
                        reasons.append("breakout!")

            # Core coin bonus
            if symbol in self.CORE_COINS:
                score += 2
                reasons.append("blue chip")

            return {
                "symbol": symbol,
                "price": ticker["last"],
                "volume": vol,
                "change_pct": change,
                "score": round(score, 1),
                "reasons": reasons,
            }

        except Exception as e:
            return None

    def print_results(self, results: list[dict]):
        """Pretty print scan results."""
        print(f"\n  {'Symbol':<12} {'Price':>10} {'24h%':>8} {'Volume':>12} {'Score':>6}  Reasons")
        print(f"  {'-'*70}")
        for r in results:
            reasons = ", ".join(r["reasons"][:3])
            print(f"  {r['symbol']:<12} {r['price']:>10.2f} {r['change_pct']:>+7.1f}% "
                  f"{r['volume']:>12,.0f} {r['score']:>6.1f}  {reasons}")
