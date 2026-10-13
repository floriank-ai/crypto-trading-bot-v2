#!/usr/bin/env python3
"""HEBEL A + C: hoehere Zeitrahmen und Trailing-only-Exits.

A) 15m / 1h / 4h — die Bewegung pro Trade skaliert etwa mit sqrt(Zeit),
   die Gebuehr ist fix. Das Verhaeltnis Edge/Kosten ist der ganze Punkt.
C) Exit-Modus "trail": KEIN festes Take-Profit, Ausstieg nur wenn der Kurs
   um X% vom besten Punkt zurueckkommt. Erzeugt den fetten rechten Rand
   (seltene grosse Gewinner), mit dem Trendsysteme hohe Kosten ueberleben.

Longs sind eingeschaltet, Regime-Gate wie im Bot (Shorts nur wenn BTC faellt,
Longs nur wenn BTC steigt). Echte Kraken-Gebuehren.
"""
import sys, os, itertools, json
os.environ["MOMENTUM_ALLOW_BREAKOUT_LONG"] = "1"
sys.path.insert(0, "/Users/floriankretschman/Downloads/crypto-trading-bot-v2")
import pandas as pd
import backtest as bt
from strategies import MomentumStrategy, Signal
from config import Config

SYMS = ["ADA/USDT","BTC/USDT","BNB/USDT","ALGO/USDT","AVAX/USDT","DOGE/USDT",
        "BCH/USDT","AAVE/USDT","DASH/USDT","ATOM/USDT","DOT/USDT","CRV/USDT",
        "DYDX/USDT","ARB/USDT","CHZ/USDT","ARKM/USDT"]
TAKER, MAKER = Config.TAKER_FEE_PCT, Config.MAKER_FEE_PCT
WARM = 50

def load_tf(tf, months=6):
    """15m holen und auf tf aggregieren (spart Abrufe)."""
    bt.TIMEFRAME = "15m"
    per = {"15m":1, "1h":4, "4h":16}[tf]
    out = {}
    for s in SYMS:
        df = bt.fetch_data_binance(s, months=months)
        if df is None or df.empty: continue
        if per > 1:
            df = df.set_index("time")
            df = df.resample(tf).agg(
                {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
            ).dropna().reset_index()
        if len(df) < WARM + 60: continue
        out[s] = df
    return out

def signals(df):
    st = MomentumStrategy(); res=[]
    for i in range(WARM, len(df)):
        sg = st.analyze(df.iloc[:i+1])
        if sg["signal"] == Signal.BUY: res.append((i,"long"))
        elif sg["signal"] == Signal.SELL: res.append((i,"short"))
    return res

def regime_ok(btc, i, sdir, bars_24h):
    if i < bars_24h: return False
    d24 = (btc[i]-btc[i-bars_24h])/btc[i-bars_24h]
    if sdir == "short": return d24 < -0.004
    return d24 > 0.004

def replay(data, sigs_by_sym, sl, tp, trail, max_bars, bars_24h, direction="both"):
    """tp=None + trail=x -> Trailing-only. Sonst festes TP."""
    tot=0.0; n=0; wins=0; ex={}
    btc = data["BTC/USDT"]["close"].tolist() if "BTC/USDT" in data else None
    for sym, df in data.items():
        close=df["close"].tolist(); high=df["high"].tolist(); low=df["low"].tolist()
        last=-1
        for idx, sdir in sigs_by_sym.get(sym, []):
            if idx <= last: continue
            if direction!="both" and sdir!=direction: continue
            if btc is not None and not regime_ok(btc, min(idx,len(btc)-1), sdir, bars_24h): continue
            entry=close[idx]
            if entry<=0: continue
            sl_px = entry*(1+sl) if sdir=="short" else entry*(1-sl)
            tp_px = (entry*(1-tp) if sdir=="short" else entry*(1+tp)) if tp else None
            best = entry
            xp=None; xk=None; xi=None
            end = min(idx+max_bars, len(close)-1) if max_bars else len(close)-1
            for j in range(idx+1, end+1):
                if sdir=="short":
                    if high[j] >= sl_px: xp,xk,xi = sl_px,"sl",j; break
                    if tp_px and low[j] <= tp_px: xp,xk,xi = tp_px,"tp",j; break
                    best = min(best, low[j])
                    if trail:
                        t_px = best*(1+trail)
                        if t_px < sl_px and high[j] >= t_px: xp,xk,xi = t_px,"trail",j; break
                else:
                    if low[j] <= sl_px: xp,xk,xi = sl_px,"sl",j; break
                    if tp_px and high[j] >= tp_px: xp,xk,xi = tp_px,"tp",j; break
                    best = max(best, high[j])
                    if trail:
                        t_px = best*(1-trail)
                        if t_px > sl_px and low[j] <= t_px: xp,xk,xi = t_px,"trail",j; break
            if xp is None: xp,xk,xi = close[end],"time",end
            g = (entry-xp)/entry if sdir=="short" else (xp-entry)/entry
            fee = TAKER + (MAKER if xk in ("tp",) else TAKER)
            net = g-fee
            tot+=net; n+=1
            if net>0: wins+=1
            ex[xk]=ex.get(xk,0)+1
            last = xi
    return {"sum":tot*100,"n":n,"wr":wins/n*100 if n else 0,
            "avg":tot/n*100 if n else 0,"ex":ex}

if __name__ == "__main__":
    print(f"Gebuehren: taker {TAKER*100:.2f}% / maker {MAKER*100:.2f}%  (6 Monate Daten)\n")
    allrows=[]
    for tf in ("1h","4h"):
        bars24 = {"15m":96,"1h":24,"4h":6}[tf]
        print(f"=== Zeitrahmen {tf} ===", flush=True)
        data = load_tf(tf)
        if not data: print("  keine Daten"); continue
        sg = {s: signals(df) for s,df in data.items()}
        tot_s = sum(len([1 for _,d in v if d=="short"]) for v in sg.values())
        tot_l = sum(len([1 for _,d in v if d=="long"]) for v in sg.values())
        print(f"  {len(data)} Symbole, {sum(len(df) for df in data.values())} Kerzen, "
              f"{tot_s} short- + {tot_l} long-Signale", flush=True)
        maxb = {"15m":672,"1h":168,"4h":42}[tf]   # jeweils ~7 Tage
        combos = []
        for sl in (0.04,0.06,0.10):
            for tp in (0.08,0.15,0.30,None):
                for tr in ((0.03,0.05,0.08) if tp is None else (None,)):
                    combos.append((sl,tp,tr))
        for d in ("both","short","long"):
            for sl,tp,tr in combos:
                r = replay(data, sg, sl, tp, tr, maxb, bars24, d)
                if r["n"] < 25: continue
                r.update({"tf":tf,"sl":sl,"tp":tp,"trail":tr,"dir":d})
                allrows.append(r)
        done=[r for r in allrows if r["tf"]==tf]
        if done:
            b=max(done,key=lambda x:x["sum"])
            print(f"  bestes {tf}: {b['dir']} SL{b['sl']*100:.0f}% "
                  f"TP{'trail'+str(int(b['trail']*100))+'%' if b['tp'] is None else str(int(b['tp']*100))+'%'} "
                  f"-> {b['n']} Trades, Summe {b['sum']:+.1f}%, Ø {b['avg']:+.3f}%", flush=True)
        print(flush=True)

    allrows.sort(key=lambda x:-x["sum"])
    print("="*94)
    print("  TOP 25 ueber alle Zeitrahmen (echte Gebuehren, n>=25)")
    print("="*94)
    print(f"  {'TF':>4}{'Richtung':>9}{'SL':>5}{'Exit':>9}{'Trades':>8}{'Summe%':>10}{'Ø/Trade':>10}{'WR':>7}")
    for r in allrows[:25]:
        ex = f"trail{int(r['trail']*100)}%" if r["tp"] is None else f"TP{int(r['tp']*100)}%"
        print(f"  {r['tf']:>4}{r['dir']:>9}{r['sl']*100:>4.0f}%{ex:>9}{r['n']:>8}"
              f"{r['sum']:>10.1f}{r['avg']:>+10.3f}{r['wr']:>6.1f}%")
    pos=[r for r in allrows if r["sum"]>0]
    print(f"\n  PROFITABEL: {len(pos)} von {len(allrows)} Varianten")
    if pos:
        print("\n  --- profitable Varianten im Detail ---")
        for r in sorted(pos,key=lambda x:-x["sum"])[:12]:
            ex = f"trail{int(r['trail']*100)}%" if r["tp"] is None else f"TP{int(r['tp']*100)}%"
            print(f"  {r['tf']:>4} {r['dir']:<6} SL{r['sl']*100:.0f}% {ex:<9} "
                  f"n={r['n']:<4} Summe {r['sum']:+7.1f}%  Ø {r['avg']:+.3f}%  WR {r['wr']:.1f}%  {r['ex']}")
    json.dump([{k:v for k,v in r.items() if k!="ex"} for r in allrows[:120]], open("/tmp/tf_sweep2.json","w"), indent=1)
