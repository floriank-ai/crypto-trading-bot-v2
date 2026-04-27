"""Tagesweiser CSV-Snapshot — kein Telegram, nur Datei.

Schreibt pro abgeschlossenem UTC-Tag genau eine Zeile nach
`logs/daily_summary.csv`. Datenquelle ist `logs/trades.json` (vom
TradeLogger ohnehin gepflegt) plus der aktuelle Portfolio-Stand vom
Cycle-Loop. Stateful: `logs/daily_summary_state.json` haelt den
Tagesanfangs-Portfolio-Wert, damit bei Restart mitten im Tag der
Anker nicht verloren geht.

Aufruf: einmal pro Cycle `DailySummary.tick(...)` aufrufen.
"""

import csv
import json
import os
from datetime import datetime, timezone, timedelta


# CSV-Header — Reihenfolge ist die Datei-Reihenfolge
COLUMNS = [
    "date_utc",
    "portfolio_sod_eur",      # Portfolio-Wert am Tagesanfang (00:00 UTC)
    "portfolio_eod_eur",      # Portfolio-Wert am Tagesende (letzter Cycle vor Rollover)
    "daily_pnl_eur",          # eod - sod
    "daily_pnl_pct",          # (eod - sod) / sod * 100
    "realized_pnl_eur",       # Summe realized_pnl aller Closes an dem Tag
    "fees_eur",               # Summe Fees aller Trades an dem Tag (open + close)
    "trades_total",           # alle log-Zeilen an dem Tag
    "trades_opened",          # buy + short-open
    "trades_closed",          # sell + cover (mit realized_pnl)
    "wins",                   # closed mit realized_pnl > 0
    "losses",                 # closed mit realized_pnl < 0
    "win_rate_pct",
    "biggest_win_eur",
    "biggest_loss_eur",
    "best_strategy",          # Strategie mit hoechstem realized_pnl an dem Tag
    "worst_strategy",         # Strategie mit niedrigstem realized_pnl an dem Tag
    "by_strategy_json",       # {strategy: {pnl, trades, wins, losses}} als JSON-String
    "open_positions_eod",     # Anzahl offener Positionen beim Rollover
]


class DailySummary:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.csv_path = os.path.join(log_dir, "daily_summary.csv")
        self.state_path = os.path.join(log_dir, "daily_summary_state.json")
        self.trades_path = os.path.join(log_dir, "trades.json")

        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(COLUMNS)

        self._state = self._load_state()

    # ── State-Helpers ────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_state(self):
        try:
            with open(self.state_path, "w") as f:
                json.dump(self._state, f, indent=2)
        except OSError as e:
            print(f"  [DailySummary] State-Save Fehler: {e}")

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Aggregation aus trades.json ──────────────────────────────────────

    def _aggregate_day(self, date_str: str) -> dict:
        """Liest trades.json und aggregiert alle Eintraege an `date_str` (UTC)."""
        empty = {
            "realized_pnl_eur": 0.0,
            "fees_eur": 0.0,
            "trades_total": 0,
            "trades_opened": 0,
            "trades_closed": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "biggest_win_eur": 0.0,
            "biggest_loss_eur": 0.0,
            "best_strategy": "",
            "worst_strategy": "",
            "by_strategy_json": "{}",
        }
        if not os.path.exists(self.trades_path):
            return empty
        try:
            with open(self.trades_path, "r") as f:
                entries = json.load(f)
        except (json.JSONDecodeError, OSError):
            return empty

        day_entries = []
        for e in entries:
            if e.get("session_start"):
                continue
            ts = e.get("timestamp", "")
            if not ts:
                continue
            try:
                # Timestamp ist datetime.now().isoformat() ohne TZ-Info.
                # Railway-Container laufen UTC → naive == UTC. Fuer lokale
                # Tests evtl. ungenau, aber gut genug fuer Production.
                dt = datetime.fromisoformat(ts)
                if dt.strftime("%Y-%m-%d") != date_str:
                    continue
            except (ValueError, TypeError):
                continue
            day_entries.append(e)

        if not day_entries:
            return empty

        realized = 0.0
        fees = 0.0
        opened = 0
        closed = 0
        wins = 0
        losses = 0
        biggest_win = 0.0
        biggest_loss = 0.0
        per_strat: dict = {}

        for e in day_entries:
            fees += float(e.get("fee_eur", 0) or 0)
            side = e.get("side", "")
            strat = e.get("strategy", "unknown") or "unknown"
            if strat not in per_strat:
                per_strat[strat] = {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
            per_strat[strat]["trades"] += 1

            if side in ("sell", "cover") and "realized_pnl" in e:
                pnl = float(e["realized_pnl"])
                realized += pnl
                closed += 1
                per_strat[strat]["pnl"] += pnl
                if pnl > 0:
                    wins += 1
                    per_strat[strat]["wins"] += 1
                    if pnl > biggest_win:
                        biggest_win = pnl
                elif pnl < 0:
                    losses += 1
                    per_strat[strat]["losses"] += 1
                    if pnl < biggest_loss:
                        biggest_loss = pnl
            else:
                opened += 1

        win_rate = (wins / closed * 100) if closed else 0.0
        # Best/Worst Strategie nach Tages-PnL
        best_strat = ""
        worst_strat = ""
        if per_strat:
            best_strat = max(per_strat.items(), key=lambda x: x[1]["pnl"])[0]
            worst_strat = min(per_strat.items(), key=lambda x: x[1]["pnl"])[0]

        # by_strategy_json: alle PnLs runden fuer Lesbarkeit
        compact = {
            s: {
                "pnl": round(d["pnl"], 2),
                "trades": d["trades"],
                "wins": d["wins"],
                "losses": d["losses"],
            }
            for s, d in per_strat.items()
        }

        return {
            "realized_pnl_eur": round(realized, 2),
            "fees_eur": round(fees, 4),
            "trades_total": len(day_entries),
            "trades_opened": opened,
            "trades_closed": closed,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(win_rate, 1),
            "biggest_win_eur": round(biggest_win, 2),
            "biggest_loss_eur": round(biggest_loss, 2),
            "best_strategy": best_strat,
            "worst_strategy": worst_strat,
            "by_strategy_json": json.dumps(compact, separators=(",", ":")),
        }

    # ── Public API ───────────────────────────────────────────────────────

    def tick(self, portfolio_value: float, open_positions_count: int):
        """Einmal pro Cycle aufrufen. Erkennt UTC-Tag-Wechsel und rollt um."""
        today = self._today_utc()
        last_date = self._state.get("last_date")
        sod_value = self._state.get("day_start_portfolio")

        # Erstaufruf: heute initialisieren, keine Zeile schreiben
        if last_date is None or sod_value is None:
            self._state["last_date"] = today
            self._state["day_start_portfolio"] = float(portfolio_value)
            self._save_state()
            return

        # Gleicher Tag → nichts tun
        if last_date == today:
            return

        # Tag-Wechsel: Zeile fuer den ABGESCHLOSSENEN Tag schreiben
        try:
            agg = self._aggregate_day(last_date)
            sod = float(sod_value)
            eod = float(portfolio_value)  # erster Cycle nach Mitternacht ~ EOD
            pnl_eur = round(eod - sod, 2)
            pnl_pct = round((eod - sod) / sod * 100, 2) if sod else 0.0

            row = [
                last_date,
                round(sod, 2),
                round(eod, 2),
                pnl_eur,
                pnl_pct,
                agg["realized_pnl_eur"],
                agg["fees_eur"],
                agg["trades_total"],
                agg["trades_opened"],
                agg["trades_closed"],
                agg["wins"],
                agg["losses"],
                agg["win_rate_pct"],
                agg["biggest_win_eur"],
                agg["biggest_loss_eur"],
                agg["best_strategy"],
                agg["worst_strategy"],
                agg["by_strategy_json"],
                int(open_positions_count),
            ]
            with open(self.csv_path, "a", newline="") as f:
                csv.writer(f).writerow(row)
            print(f"  [DailySummary] Zeile geschrieben fuer {last_date}: "
                  f"P&L {pnl_eur:+.2f}EUR ({pnl_pct:+.2f}%), "
                  f"{agg['trades_closed']} Closes, "
                  f"Win-Rate {agg['win_rate_pct']:.1f}%")
        except Exception as e:
            print(f"  [DailySummary] Aggregations-Fehler fuer {last_date}: {e}")

        # Neuen Tag anfangen
        self._state["last_date"] = today
        self._state["day_start_portfolio"] = float(portfolio_value)
        self._save_state()
