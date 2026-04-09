import csv
import json
import os
from datetime import datetime


class TradeLogger:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.csv_path = os.path.join(log_dir, "trades.csv")
        self.json_path = os.path.join(log_dir, "trades.json")

        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "pair", "side", "volume", "price_eur",
                    "cost_eur", "fee_eur", "total_eur", "mode",
                    "strategy", "signal_reason", "balance_after",
                ])

    def log_session_start(self, initial_capital: float):
        """Write a session start marker so portfolio_status.py knows where current session begins."""
        trades = []
        if os.path.exists(self.json_path):
            with open(self.json_path, "r") as f:
                try:
                    trades = json.load(f)
                except json.JSONDecodeError:
                    trades = []
        trades.append({
            "session_start": True,
            "timestamp": datetime.now().isoformat(),
            "initial_capital": initial_capital,
        })
        with open(self.json_path, "w") as f:
            json.dump(trades, f, indent=2)

    def log_trade(self, pair: str, side: str, volume: float, price: float,
                  cost: float, fee: float, mode: str, strategy: str = "",
                  signal_reason: str = "", balance_after: float = 0):
        timestamp = datetime.now().isoformat()
        total = cost + fee if side == "buy" else cost - fee

        with open(self.csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp, pair, side, f"{volume:.8f}", f"{price:.2f}",
                f"{cost:.4f}", f"{fee:.4f}", f"{total:.4f}", mode,
                strategy, signal_reason, f"{balance_after:.2f}",
            ])

        trade = {
            "timestamp": timestamp, "pair": pair, "side": side,
            "volume": volume, "price_eur": price, "cost_eur": cost,
            "fee_eur": fee, "total_eur": total, "mode": mode,
            "strategy": strategy, "signal_reason": signal_reason,
            "balance_after": balance_after,
        }

        trades = []
        if os.path.exists(self.json_path):
            with open(self.json_path, "r") as f:
                try:
                    trades = json.load(f)
                except json.JSONDecodeError:
                    trades = []
        trades.append(trade)
        with open(self.json_path, "w") as f:
            json.dump(trades, f, indent=2)

        emoji = "BUY" if side == "buy" else "SELL"
        print(f"  >> {emoji} {volume:.8f} {pair} @ {price:.2f}EUR [{strategy}] (fee: {fee:.4f}EUR)")

    def get_summary(self) -> dict:
        if not os.path.exists(self.json_path):
            return {"total_trades": 0, "realized_pnl": 0, "total_fees_eur": 0, "strategies": {}}
        with open(self.json_path, "r") as f:
            all_entries = json.load(f)

        # Nur aktuelle Session
        si = max((i for i, e in enumerate(all_entries) if e.get("session_start")), default=-1)
        trades = [e for e in all_entries[si+1:] if not e.get("session_start")]

        if not trades:
            return {"total_trades": 0, "realized_pnl": 0, "total_fees_eur": 0, "strategies": {}}

        # P&L: Einnahmen (sell/cover) minus Ausgaben (buy/short)
        money_in  = sum(t["total_eur"] for t in trades if t["side"] in ("sell", "cover"))
        money_out = sum(t["total_eur"] for t in trades if t["side"] in ("buy", "short"))
        fees = sum(t["fee_eur"] for t in trades)

        strats = {}
        for t in trades:
            s = t.get("strategy", "unknown")
            if s not in strats:
                strats[s] = {"count": 0}
            strats[s]["count"] += 1

        return {
            "total_trades": len(trades),
            "total_fees_eur": round(fees, 4),
            "realized_pnl": round(money_in - money_out, 2),
            "strategies": strats,
        }
