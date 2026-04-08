from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import pandas as pd
from datetime import datetime, timedelta
from config import Config


# Assets to trade
ASSETS = {
    "gold":    {"symbol": "GLD",  "name": "Gold ETF",            "alloc": 0.15},
    "quantum": {"symbol": "IONQ", "name": "IonQ Quantum",        "alloc": 0.08},
    "quantum2":{"symbol": "RGTI", "name": "Rigetti Quantum",     "alloc": 0.07},
    "etf_tech":{"symbol": "QQQ",  "name": "Nasdaq 100 ETF",      "alloc": 0.00},  # optional
}

SYMBOLS = [a["symbol"] for a in ASSETS.values()]


class AlpacaExchange:
    def __init__(self):
        self.paper = Config.is_paper_mode()
        self.client = TradingClient(
            Config.ALPACA_API_KEY,
            Config.ALPACA_API_SECRET,
            paper=self.paper,
        )
        self.data_client = StockHistoricalDataClient(
            Config.ALPACA_API_KEY,
            Config.ALPACA_API_SECRET,
        )
        self._balance_cache = None

    def get_balance(self) -> float:
        try:
            account = self.client.get_account()
            return float(account.cash)
        except Exception as e:
            print(f"  [Alpaca] Balance error: {e}")
            return 0.0

    def get_ticker(self, symbol: str) -> dict:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=datetime.utcnow() - timedelta(minutes=5),
            )
            bars = self.data_client.get_stock_bars(req).df
            if bars.empty:
                return {}
            last = bars["close"].iloc[-1]
            return {"last": last, "ask": last, "bid": last}
        except Exception as e:
            print(f"  [Alpaca] Ticker error {symbol}: {e}")
            return {}

    def get_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 100) -> pd.DataFrame:
        try:
            tf_map = {"1m": TimeFrame.Minute, "5m": TimeFrame.Minute,
                      "15m": TimeFrame.Minute, "1h": TimeFrame.Hour, "1d": TimeFrame.Day}
            tf = tf_map.get(timeframe, TimeFrame.Minute)
            minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}.get(timeframe, 15)

            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=datetime.utcnow() - timedelta(minutes=minutes * limit),
            )
            bars = self.data_client.get_stock_bars(req).df

            if bars.empty:
                return pd.DataFrame()

            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.loc[symbol]

            bars = bars.reset_index()
            bars = bars.rename(columns={"timestamp": "time", "open": "open",
                                        "high": "high", "low": "low",
                                        "close": "close", "volume": "volume"})
            return bars[["time", "open", "high", "low", "close", "volume"]].tail(limit)
        except Exception as e:
            print(f"  [Alpaca] OHLCV error {symbol}: {e}")
            return pd.DataFrame()

    def place_order(self, symbol: str, side: str, qty: float) -> dict:
        try:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=round(qty, 4),
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = self.client.submit_order(req)
            return {
                "status": "ok",
                "txid": str(order.id),
                "price": 0,  # filled price comes async
                "cost": 0,
                "fee": 0,
            }
        except Exception as e:
            print(f"  [Alpaca] Order error {symbol}: {e}")
            return {"status": "error", "error": str(e)}

    def get_positions(self) -> dict:
        try:
            positions = self.client.get_all_positions()
            return {p.symbol: {"qty": float(p.qty), "avg_price": float(p.avg_entry_price),
                               "market_value": float(p.market_value),
                               "unrealized_pnl": float(p.unrealized_pl)} for p in positions}
        except Exception as e:
            print(f"  [Alpaca] Positions error: {e}")
            return {}

    def is_market_open(self) -> bool:
        try:
            clock = self.client.get_clock()
            return clock.is_open
        except Exception:
            return False
