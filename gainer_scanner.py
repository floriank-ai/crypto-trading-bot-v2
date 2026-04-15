"""
Gainer Scanner: scans Binance public API for top 24h gainers.
No API key needed — only reads public market data.
"""
import ccxt
import pandas as pd


class GainerScanner:
    """Scans Binance for top gainers with momentum confirmation."""

    def __init__(self):
        # KuCoin: keine Geo-Sperre, globale Verfügbarkeit, gleiche USDT-Paare wie Binance
        self.binance = ccxt.kucoin({"enableRateLimit": True})
        self._markets_loaded = False

    def _ensure_markets(self):
        if not self._markets_loaded:
            try:
                self.binance.load_markets()
                self._markets_loaded = True
            except Exception as e:
                print(f"  [GainerScanner] Markets error: {e}")

    def get_top_gainers(self, min_gain_pct: float = 15.0, max_results: int = 5) -> list[dict]:
        """
        Returns USDT pairs sorted by 24h gain.
        Filters:
          - 24h gain >= min_gain_pct
          - Volume >= 1M USDT (liquid enough)
          - 1h trend still positive (not already peaked)
        """
        try:
            self._ensure_markets()
            tickers = self.binance.fetch_tickers()

            candidates = []
            for symbol, t in tickers.items():
                if not symbol.endswith("/USDT"):
                    continue
                pct = t.get("percentage") or 0
                vol = t.get("quoteVolume") or 0
                last = t.get("last") or 0
                if pct >= min_gain_pct and vol >= 1_000_000 and last > 0:
                    candidates.append({
                        "symbol": symbol,
                        "gain_24h": round(pct, 2),
                        "volume_usdt": int(vol),
                        "price": last,
                    })

            # Sort by 24h gain descending
            candidates.sort(key=lambda x: x["gain_24h"], reverse=True)

            # Check 1h trend for top candidates (stop at first max_results passing)
            results = []
            for coin in candidates[:20]:
                if len(results) >= max_results:
                    break
                trend_1h = self._get_1h_trend(coin["symbol"])
                coin["trend_1h"] = round(trend_1h * 100, 2)
                if trend_1h > 0:
                    results.append(coin)
                    print(f"  [Gainer] {coin['symbol']} +{coin['gain_24h']}% "
                          f"(1h: {trend_1h*100:+.1f}%) ✓")
                else:
                    print(f"  [Gainer] {coin['symbol']} +{coin['gain_24h']}% "
                          f"(1h: {trend_1h*100:+.1f}%) — peaked, skip")
            return results

        except Exception as e:
            print(f"  [GainerScanner] Error: {e}")
            return []

    def _get_1h_trend(self, symbol: str) -> float:
        """1h momentum: positive = still going up, negative = peaked."""
        try:
            ohlcv = self.binance.fetch_ohlcv(symbol, "1h", limit=3)
            if not ohlcv or len(ohlcv) < 2:
                return 0
            return (ohlcv[-1][4] - ohlcv[-2][4]) / ohlcv[-2][4]
        except Exception:
            return 0

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 100) -> pd.DataFrame:
        """Fetch OHLCV from Binance for strategy analysis."""
        try:
            ohlcv = self.binance.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv:
                return pd.DataFrame()
            df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            return df
        except Exception as e:
            print(f"  [GainerScanner] OHLCV error {symbol}: {e}")
            return pd.DataFrame()

    def get_price(self, symbol: str) -> float:
        """Current Binance price for a gainer symbol."""
        try:
            ticker = self.binance.fetch_ticker(symbol)
            return ticker.get("last") or 0
        except Exception:
            return 0

    def get_ticker(self, symbol: str) -> dict:
        """Binance ticker (ask/bid/last) for a gainer symbol."""
        try:
            t = self.binance.fetch_ticker(symbol)
            return {
                "ask": t.get("ask", 0),
                "bid": t.get("bid", 0),
                "last": t.get("last", 0),
                "volume": t.get("quoteVolume", 0),
                "change_pct": t.get("percentage", 0),
            }
        except Exception as e:
            print(f"  [GainerScanner] Ticker error {symbol}: {e}")
            return {}
