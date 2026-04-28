import time
import json
import os
import ccxt
import pandas as pd
from config import Config


class Exchange:
    def __init__(self):
        self.exchange = ccxt.kraken({
            "apiKey": Config.KRAKEN_API_KEY if not Config.is_paper_mode() else "",
            "secret": Config.KRAKEN_API_SECRET if not Config.is_paper_mode() else "",
            "enableRateLimit": True,
        })

        # Paper trading state — restore from log if available
        self.paper_balance = Config.INITIAL_CAPITAL
        self.paper_positions = {}
        self._markets_loaded = False
        if os.getenv("RESET_PAPER_BALANCE") == "1":
            self._reset_paper_state()
        else:
            self._restore_paper_balance()

    def _reset_paper_state(self):
        """Hard reset: wipe logs and start fresh with INITIAL_CAPITAL.

        28.04.2026: Erweitert um daily_summary_state.json (sonst hängt Tagesanker
        von gestern fest → falscher Tages-P&L) und paper_short_positions.
        """
        wipe_files = [
            (os.path.join("logs", "trades.json"), "[]"),
            (os.path.join("logs", "positions.json"), "{}"),
            (os.path.join("logs", "daily_summary_state.json"), "{}"),
        ]
        for path, content in wipe_files:
            try:
                os.makedirs("logs", exist_ok=True)
                with open(path, "w") as f:
                    f.write(content)
            except Exception:
                pass
        self.paper_balance = Config.INITIAL_CAPITAL
        self.paper_positions = {}
        if hasattr(self, "paper_short_positions"):
            self.paper_short_positions = {}
        print(f"  [Reset] Paper Trading zurückgesetzt auf {Config.INITIAL_CAPITAL:.2f} EUR")
        print(f"  [Reset] Wiped: trades.json, positions.json, daily_summary_state.json")

    def _restore_paper_balance(self):
        """Restore paper balance from trades.json on restart (for persistent deployments)."""
        if not Config.is_paper_mode():
            return
        json_path = os.path.join("logs", "trades.json")
        if not os.path.exists(json_path):
            return
        try:
            with open(json_path) as f:
                entries = json.load(f)
            trades = [e for e in entries if not e.get("session_start") and "balance_after" in e]
            if trades:
                self.paper_balance = trades[-1]["balance_after"]
                print(f"  [Restore] Paper balance restored: {self.paper_balance:.2f} EUR")
        except Exception as e:
            print(f"  [Restore] Could not restore balance: {e}")

    def _ensure_markets(self):
        if not self._markets_loaded:
            try:
                self.exchange.load_markets()
                self._markets_loaded = True
            except Exception as e:
                print(f"  Warning: Could not load markets: {e}")

    def get_all_eur_pairs(self) -> list[str]:
        """Get all tradeable EUR pairs on Kraken."""
        self._ensure_markets()
        pairs = []
        for symbol in self.exchange.symbols:
            if symbol.endswith("/EUR") and self.exchange.markets[symbol].get("active", True):
                pairs.append(symbol)
        return sorted(pairs)

    def has_eur_pair(self, base: str) -> bool:
        """
        Checks whether Kraken lists BASE/EUR as an active market.
        Used by the Mega-Gainer-Alarm to tell dir, ob der Bot den Coin
        ueberhaupt handeln koennte (Kraken Paper/Live) oder ob es
        reine Info ist.
        """
        try:
            self._ensure_markets()
            sym = f"{base}/EUR"
            market = self.exchange.markets.get(sym)
            if not market:
                return False
            return bool(market.get("active", True))
        except Exception:
            return False

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 100) -> pd.DataFrame:
        """Fetch OHLCV candle data."""
        try:
            self._ensure_markets()
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv:
                return pd.DataFrame()

            df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            return df
        except Exception as e:
            print(f"  Error OHLCV {symbol}: {e}")
            return pd.DataFrame()

    def get_ticker(self, symbol: str) -> dict:
        """Get current ticker."""
        try:
            self._ensure_markets()
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                "ask": ticker["ask"],
                "bid": ticker["bid"],
                "last": ticker["last"],
                "volume": ticker.get("quoteVolume", 0),
                "change_pct": ticker.get("percentage", 0),
            }
        except Exception as e:
            print(f"  Error ticker {symbol}: {e}")
            return {}

    def get_tickers_bulk(self, symbols: list[str]) -> dict:
        """Get tickers for multiple symbols at once."""
        result = {}
        try:
            self._ensure_markets()
            tickers = self.exchange.fetch_tickers(symbols)
            for sym, t in tickers.items():
                result[sym] = {
                    "ask": t.get("ask", 0),
                    "bid": t.get("bid", 0),
                    "last": t.get("last", 0),
                    "volume": t.get("quoteVolume", 0),
                    "change_pct": t.get("percentage", 0),
                }
        except Exception as e:
            print(f"  Error bulk tickers: {e}")
        return result

    def get_balance(self) -> float:
        """Get available EUR balance."""
        if Config.is_paper_mode():
            return self.paper_balance
        try:
            balance = self.exchange.fetch_balance()
            return float(balance.get("EUR", {}).get("free", 0))
        except Exception as e:
            print(f"  Error balance: {e}")
            return 0.0

    def get_min_order(self, symbol: str) -> float:
        """Get minimum order size for a symbol."""
        self._ensure_markets()
        market = self.exchange.markets.get(symbol, {})
        limits = market.get("limits", {}).get("amount", {})
        return limits.get("min", 0.0001)

    def place_order(self, symbol: str, side: str, volume: float,
                    price: float = None, direction: str = "long") -> dict:
        """Place a market order."""
        if Config.is_paper_mode():
            return self._paper_order(symbol, side, volume, direction)

        try:
            order = self.exchange.create_market_order(symbol, side, volume)
            return {
                "status": "ok",
                "txid": [order["id"]],
                "price": order.get("average", order.get("price", 0)),
                "cost": order.get("cost", 0),
                "fee": order.get("fee", {}).get("cost", 0),
            }
        except Exception as e:
            print(f"  Order error: {e}")
            return {"status": "error", "error": str(e)}

    def _paper_order(self, symbol: str, side: str, volume: float,
                     direction: str = "long") -> dict:
        """Simulate order in paper mode. Supports long and short."""
        ticker = self.get_ticker(symbol)
        if not ticker or not ticker.get("last"):
            return {"status": "error", "error": "No price data"}

        exec_price = ticker["ask"] if side == "buy" else ticker["bid"]
        if not exec_price:
            exec_price = ticker["last"]

        cost = volume * exec_price
        fee = cost * 0.0026

        if direction == "short":
            # Short öffnen: Margin reservieren (cost), Gewinn wenn Preis fällt
            if side == "sell":  # Short eröffnen
                margin = cost * 0.20 + fee  # 20% Margin + Fee
                if margin > self.paper_balance:
                    return {"status": "error", "error": f"Not enough margin"}
                self.paper_balance -= margin
                self.paper_short_positions = getattr(self, "paper_short_positions", {})
                self.paper_short_positions[symbol] = {
                    "volume": volume, "entry_price": exec_price, "margin": margin
                }
            else:  # Short schließen (buy to cover)
                shorts = getattr(self, "paper_short_positions", {})
                if symbol not in shorts:
                    return {"status": "error", "error": "No short position"}
                entry = shorts[symbol]["entry_price"]
                margin = shorts[symbol]["margin"]
                pnl = (entry - exec_price) * volume  # Gewinn wenn Preis gefallen
                self.paper_balance += margin + pnl - fee
                del shorts[symbol]
        else:
            if side == "buy":
                total = cost + fee
                if total > self.paper_balance:
                    return {"status": "error", "error": f"Balance {self.paper_balance:.2f}EUR < {total:.2f}EUR"}
                self.paper_balance -= total
                self.paper_positions[symbol] = self.paper_positions.get(symbol, 0) + volume
            else:
                held = self.paper_positions.get(symbol, 0)
                if volume > held * 1.001:
                    return {"status": "error", "error": f"Position {held} < {volume}"}
                self.paper_balance += cost - fee
                self.paper_positions[symbol] = max(0, held - volume)
                if self.paper_positions[symbol] < 0.00000001:
                    del self.paper_positions[symbol]

        return {
            "status": "ok",
            "txid": [f"PAPER-{int(time.time())}"],
            "price": exec_price,
            "cost": cost,
            "fee": fee,
        }
