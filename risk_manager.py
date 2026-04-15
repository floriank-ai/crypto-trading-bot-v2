import json
import os
from config import Config

POSITIONS_PATH = os.path.join("logs", "positions.json")


class RiskManager:
    def __init__(self):
        self.max_risk = Config.MAX_RISK_PER_TRADE
        self.stop_loss_pct = Config.STOP_LOSS_PCT
        self.take_profit_pct = Config.TAKE_PROFIT_PCT
        self.max_positions = Config.MAX_OPEN_POSITIONS
        self.open_positions = {}
        self.daily_start_value = Config.INITIAL_CAPITAL

    # ── Tages-Ziel ──────────────────────────────────────────────────────────

    def get_daily_pnl_pct(self, exchange) -> float:
        """Current P&L vs. daily start value in percent."""
        current = self.get_portfolio_value(exchange)
        return (current - self.daily_start_value) / self.daily_start_value * 100

    def get_trading_phase(self, exchange) -> str:
        """
        Phase based on daily P&L vs. 5% target:
          'aggressive'   → < 2%   : volle Offensive, Rotation aktiv
          'normal'       → 2-4%   : normal weitermachen
          'protect'      → 4-5%   : keine neuen Trades, SL nachziehen
        Bei Erreichen von 5%: auto-reset → neues Ziel auf aktuellem Stand.
        """
        pnl = self.get_daily_pnl_pct(exchange)
        target = Config.DAILY_TARGET_PCT
        if pnl >= target:
            self.reset_daily_target(exchange)
            return "aggressive"
        if pnl >= target * 0.80:
            return "protect"
        if pnl >= target * 0.40:
            return "normal"
        return "aggressive"

    def reset_daily_target(self, exchange):
        """Setzt den Startpunkt neu — nächste 5% vom aktuellen Stand."""
        new_base = self.get_portfolio_value(exchange)
        print(f"\n  *** 5% ZIEL ERREICHT! Neuer Startpunkt: {new_base:.2f}EUR → nächstes Ziel: {new_base * (1 + Config.DAILY_TARGET_PCT/100):.2f}EUR ***\n")
        self.daily_start_value = new_base

    # ── Trailing Stop-Loss ───────────────────────────────────────────────────

    def update_trailing_stop(self, symbol: str, current_price: float):
        """Nachziehen des Stop-Loss sobald Position im Gewinn."""
        if symbol not in self.open_positions:
            return
        pos = self.open_positions[symbol]
        direction = pos.get("direction", "long")
        entry = pos["entry_price"]

        if direction == "long":
            pnl_pct = (current_price - entry) / entry
            if pnl_pct >= 0.14:
                # Ab +14%: SL 4% unter aktuellem Preis nachziehen
                new_sl = current_price * (1 - 0.04)
                if new_sl > pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    self._save_positions()
            elif pnl_pct >= 0.09:
                # Ab +9%: SL auf Break-Even
                new_sl = entry * 1.001
                if new_sl > pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    self._save_positions()
        else:  # short
            pnl_pct = (entry - current_price) / entry
            if pnl_pct >= 0.14:
                new_sl = current_price * (1 + 0.04)
                if new_sl < pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    self._save_positions()
            elif pnl_pct >= 0.09:
                new_sl = entry * 0.999
                if new_sl < pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                    self._save_positions()

    def calculate_position_size(self, balance: float, price: float, strategy: str = "momentum",
                                 dca_multiplier: float = 1.0, leverage: int = 1) -> float:
        """Calculate position size. DCA uses fixed EUR amount, others use risk %."""
        if strategy == "gainer":
            # 10% of portfolio — caller (execute_gainer_trade) computes this directly
            amount_eur = balance * Config.GAINER_SLOT_PCT
            return round(amount_eur / price, 8) if price > 0 else 0

        if strategy == "dca":
            amount_eur = Config.DCA_AMOUNT_EUR * dca_multiplier
            amount_eur = min(amount_eur, balance * 0.20)  # never more than 20% of balance
            return round(amount_eur / price, 8) if price > 0 else 0

        if strategy == "grid":
            position_value = balance * 0.15  # 15% per grid level
            return round(position_value / price, 8) if price > 0 else 0

        # Momentum / sentiment: aggressive sizing + leverage
        risk_amount = balance * self.max_risk
        position_value = risk_amount / self.stop_loss_pct
        max_position = balance * 0.20  # max 20% pro Position
        position_value = min(position_value, max_position)
        position_value *= leverage
        position_value = min(position_value, balance * 0.20)  # auch mit Hebel max 20%
        return round(position_value / price, 8) if price > 0 else 0

    MAX_DCA_POSITIONS = 3
    MAX_GAINER_POSITIONS = 1  # always exactly 1 gainer slot
    MAX_SHORT_POSITIONS = 6  # bis zu 50% Shorts erlaubt

    def get_weakest_position(self, exchange) -> str | None:
        """Return the symbol of the worst-performing open position (for rotation)."""
        worst_sym = None
        worst_pnl = float("inf")
        for symbol, pos in self.open_positions.items():
            ticker = exchange.get_ticker(symbol)
            if not ticker or not ticker.get("last"):
                continue
            current = ticker["last"]
            direction = pos.get("direction", "long")
            if direction == "short":
                pnl_pct = (pos["entry_price"] - current) / pos["entry_price"]
            else:
                pnl_pct = (current - pos["entry_price"]) / pos["entry_price"]
            if pnl_pct < worst_pnl:
                worst_pnl = pnl_pct
                worst_sym = symbol
        return worst_sym

    def can_open_position(self, symbol: str, strategy: str = "momentum",
                          direction: str = "long") -> bool:
        if symbol in self.open_positions:
            return False
        if len(self.open_positions) >= self.max_positions:
            return False
        if strategy == "gainer":
            gainer_count = sum(1 for p in self.open_positions.values() if p.get("strategy") == "gainer")
            if gainer_count >= self.MAX_GAINER_POSITIONS:
                return False
        if strategy == "dca":
            dca_count = sum(1 for p in self.open_positions.values() if p.get("strategy") == "dca")
            if dca_count >= self.MAX_DCA_POSITIONS:
                return False
        if direction == "short":
            short_count = sum(1 for p in self.open_positions.values() if p.get("direction") == "short")
            if short_count >= self.MAX_SHORT_POSITIONS:
                return False
        return True

    def open_position(self, symbol: str, price: float, volume: float,
                      strategy: str = "momentum", direction: str = "long"):
        """Track a new position with strategy-aware SL/TP. direction: long or short."""
        sl_pct = self.stop_loss_pct * 1.5 if strategy == "sentiment" else self.stop_loss_pct
        tp_pct = self.take_profit_pct * 1.5 if strategy == "sentiment" else self.take_profit_pct

        if strategy == "gainer":
            sl_pct = Config.GAINER_SL_PCT   # tight SL: meme coins dump hard
            tp_pct = Config.GAINER_TP_PCT   # high TP: these can run far

        if strategy == "grid":
            sl_pct = Config.GRID_SPREAD_PCT
            tp_pct = Config.GRID_SPREAD_PCT

        if strategy == "dca":
            sl_pct = 0.12
            tp_pct = 0.35

        # Short: SL nach oben, TP nach unten
        if direction == "short":
            stop_loss  = price * (1 + sl_pct)
            take_profit = price * (1 - tp_pct)
        else:
            stop_loss  = price * (1 - sl_pct)
            take_profit = price * (1 + tp_pct)

        margin = price * volume * 0.20 if direction == "short" else price * volume

        self.open_positions[symbol] = {
            "entry_price": price,
            "volume": volume,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy": strategy,
            "direction": direction,
            "margin": margin,
        }
        self._save_positions()

    def check_exit(self, symbol: str, current_price: float) -> str | None:
        if symbol not in self.open_positions:
            return None
        self.update_trailing_stop(symbol, current_price)
        pos = self.open_positions[symbol]
        direction = pos.get("direction", "long")

        if direction == "short":
            if current_price >= pos["stop_loss"]:
                return "stop_loss"
            if current_price <= pos["take_profit"]:
                return "take_profit"
        else:
            if current_price <= pos["stop_loss"]:
                return "stop_loss"
            if current_price >= pos["take_profit"]:
                return "take_profit"
        return None

    def close_position(self, symbol: str) -> dict | None:
        result = self.open_positions.pop(symbol, None)
        self._save_positions()
        return result

    def _save_positions(self):
        """Persist open positions to disk so restarts don't lose them."""
        os.makedirs("logs", exist_ok=True)
        try:
            with open(POSITIONS_PATH, "w") as f:
                json.dump(self.open_positions, f, indent=2)
        except Exception as e:
            print(f"  [Positions] Save error: {e}")

    def get_portfolio_value(self, exchange) -> float:
        """Calculate total portfolio value including open positions."""
        total = exchange.get_balance()
        for symbol, pos in self.open_positions.items():
            ticker = exchange.get_ticker(symbol)
            if not ticker or not ticker.get("last"):
                continue
            current_price = ticker["last"]
            direction = pos.get("direction", "long")
            if direction == "short":
                pnl = (pos["entry_price"] - current_price) * pos["volume"]
                total += pos["margin"] + pnl
            else:
                total += pos["volume"] * current_price
        return total
