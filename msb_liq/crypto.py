"""Crypto-only version: MSB + liquidity sweep + Michael's bands on USDT perpetuals.

Data: Binance USD-M futures 1h candles, 2020 -> Nov 2026, for BTC ETH SOL BNB XRP
(data/crypto/*.csv.gz). Costs: 0.05% taker fee + 0.02% slippage per side, and
0.01% funding every 8h while a trade is open.

    python -m msb_liq.crypto optimize 1   # stage 1: plain idea; `optimize 2` adds trend filter
                                          # rank settings on 2020-2023, check them on 2024-2026
    python -m msb_liq.crypto run          # chosen settings: per coin / per year report + chart
"""
import itertools
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .strategy import Params, backtest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "crypto"
OUT = ROOT / "results" / "crypto"
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
SPLIT = pd.Timestamp("2024-01-01", tz="UTC")   # before = in-sample, after = out-of-sample

# The settings chosen by `optimize` (ranked on 2020-2023 only).
CRYPTO = Params(
    band_fast=21, band_slow=50, swing_len=5, msb_window=5, rr=2.0,
    min_risk_pct=1.0, max_leverage=5.0, risk_pct=1.0,
    commission_pct=0.07, funding_pct=0.01,
    entry_start_hour=9, entry_end_hour=22,
    flat_hour=99,   # entries only 09-22 Plovdiv, but SL/TP orders stay on the exchange overnight
)

# Stage 1: the plain MSB + liq + bands idea (see results/crypto/optimize_stage1.txt)
GRID1 = {
    "band": [(12, 21), (21, 50), (50, 200)],
    "swing_len": [2, 3, 5],
    "rr": [1.5, 2.0, 3.0],
    "msb_window": [5, 10],
    "min_risk_pct": [0.5, 1.0],
    "flat_hour": [23, 99],   # 23 = flat before midnight, 99 = leave SL/TP running overnight
}
# Stage 2: add a higher-timeframe trend filter and a long-only option
GRID2 = {
    "band": [(12, 21), (21, 50)],
    "swing_len": [3, 5, 8],
    "rr": [2.0, 3.0],
    "msb_window": [5, 10],
    "flat_hour": [23, 99],
    "trend_ema": [0, 480, 1200],      # 1h bars: 480 = 20-day EMA, 1200 = 50-day EMA
    "allow_short": [True, False],
}
# Stage 3: same idea on 4h candles (wider stops -> fees are a much smaller part of each R)
GRID3 = {
    "tf": ["4h"],
    "band": [(12, 21), (21, 50)],
    "swing_len": [2, 3, 5],
    "rr": [2.0, 3.0],
    "msb_window": [3, 6],
    "flat_hour": [99],
    "trend_ema": [0, 300],            # 4h bars: 300 = 50-day EMA
    "allow_short": [True, False],
}


def load(sym: str, tf: str = "1h") -> pd.DataFrame:
    d = pd.read_csv(DATA / f"{sym}_1h.csv.gz")
    d.index = pd.to_datetime(d["open_time"], unit="s", utc=True)
    d = d[["open", "high", "low", "close"]].astype(float)
    if tf != "1h":
        d = d.resample(tf).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return d


_FRAMES: dict = {}


def _frames(tf="1h"):
    if tf not in _FRAMES:
        _FRAMES[tf] = {s: load(s, tf) for s in COINS}
    return _FRAMES[tf]


def _eval(kw: dict) -> dict:
    band = kw.pop("band")
    tf = kw.pop("tf", "1h")
    p = replace(CRYPTO, band_fast=band[0], band_slow=band[1], **kw)
    is_r, oos_r = [], []
    for df in _frames(tf).values():
        for t in backtest(df, p).trades:
            (is_r if t.entry_time < SPLIT else oos_r).append(t.r)
    is_r, oos_r = np.array(is_r), np.array(oos_r)
    return {"tf": tf, "band": f"{band[0]}/{band[1]}", **kw,
            "is_trades": len(is_r), "is_avgR": is_r.mean(), "is_win": (is_r > 0).mean(),
            "oos_trades": len(oos_r), "oos_avgR": oos_r.mean(), "oos_win": (oos_r > 0).mean()}


def optimize(stage: str = "3"):
    grid = {"1": GRID1, "2": GRID2, "3": GRID3}[stage]
    combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]
    with ProcessPoolExecutor() as ex:
        rows = list(ex.map(_eval, combos, chunksize=4))
    tab = pd.DataFrame(rows).sort_values("is_avgR", ascending=False)
    OUT.mkdir(parents=True, exist_ok=True)
    tab.to_csv(OUT / f"optimize_stage{stage}_all.csv", index=False)
    pd.set_option("display.width", 220)
    lines = [
        f"configs tested: {len(tab)}",
        f"positive in-sample (2020-23):       {(tab.is_avgR > 0).mean():.0%}",
        f"positive out-of-sample (2024-26):   {(tab.oos_avgR > 0).mean():.0%}",
        f"rank correlation IS vs OOS avgR:    {tab.is_avgR.rank().corr(tab.oos_avgR.rank()):.2f}",
        "", "Top 20 by in-sample avg R (OOS columns were NOT used to pick):",
        tab.head(20).round(3).to_string(index=False),
    ]
    txt = "\n".join(lines)
    (OUT / f"optimize_stage{stage}.txt").write_text(txt + "\n")
    print(txt)


def run(p: Params = CRYPTO, balance: float = 1000.0):
    OUT.mkdir(parents=True, exist_ok=True)
    allt, curves = [], {}
    for sym in COINS:
        res = backtest(load(sym), p, balance)
        curves[sym] = res.equity
        for t in res.trades:
            allt.append({"coin": sym, **asdict(t)})
    t = pd.DataFrame(allt)
    t["year"] = pd.to_datetime(t.entry_time, utc=True).dt.year
    t.to_csv(OUT / "trades.csv", index=False)

    def summ(g):
        w = g[g.pnl > 0].pnl.sum()
        l = -g[g.pnl <= 0].pnl.sum()
        return pd.Series({"trades": len(g), "win_%": round((g.r > 0).mean() * 100, 1),
                          "avg_R": round(g.r.mean(), 3), "sum_R": round(g.r.sum(), 1),
                          "PF": round(w / l, 2) if l else np.inf})

    by_coin = t.groupby("coin").apply(summ, include_groups=False)
    by_year = t.groupby("year").apply(summ, include_groups=False)
    eq = pd.DataFrame({s: c for s, c in curves.items()})
    fin = eq.ffill().iloc[-1]
    by_coin["end_eur"] = fin.round(0)
    by_coin["max_dd_%"] = ((eq / eq.cummax() - 1).min() * 100).round(1)
    total = summ(t)

    # Portfolio view: one €1000 account, one trade at a time, taking signals from all coins
    # in time order (first signal wins). Compounded at risk_pct per trade, using each trade's R.
    t2 = t.sort_values("entry_time")
    bal, busy_until, taken, curve = balance, pd.Timestamp(0, tz="UTC"), [], []
    for _, row in t2.iterrows():
        et, xt = pd.Timestamp(row.entry_time), pd.Timestamp(row.exit_time)
        if et < busy_until:
            continue
        bal *= 1 + row.r * p.risk_pct / 100
        busy_until = xt
        taken.append(row)
        curve.append((xt, bal))
    pc = pd.Series([b for _, b in curve], index=[x for x, _ in curve])
    port = pd.DataFrame(taken)
    port_dd = (pc / pc.cummax() - 1).min() * 100
    yearly = pc.groupby(pc.index.year).last()
    yearly_ret = (yearly / yearly.shift(1, fill_value=balance) - 1) * 100

    pd.set_option("display.width", 200)
    out = [
        "Settings: " + ", ".join(f"{k}={v}" for k, v in asdict(p).items()),
        "", "== Per coin (each its own EUR 1000 account) ==", by_coin.to_string(),
        "", "== Per year (all coins) ==", by_year.to_string(),
        "", f"== All trades: {int(total.trades)} trades, win {total['win_%']}%, avg {total.avg_R:+.3f}R, PF {total.PF} ==",
        "", "== One EUR 1000 account trading all 5 coins, max 1 open trade ==",
        f"trades taken {len(port)} | end balance EUR {bal:,.0f} | max drawdown {port_dd:.1f}%",
        "yearly return %: " + ", ".join(f"{y}: {v:+.1f}" for y, v in yearly_ret.round(1).items()),
        f"out-of-sample only (2024+): {len(port[pd.to_datetime(port.entry_time, utc=True) >= SPLIT])} trades, "
        f"avg {port[pd.to_datetime(port.entry_time, utc=True) >= SPLIT].r.mean():+.3f}R",
    ]
    txt = "\n".join(out)
    (OUT / "summary.txt").write_text(txt + "\n")
    print(txt)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
        for s in COINS:
            ax[0].plot(eq[s].dropna().index, eq[s].dropna().values, label=s, lw=1.1)
        ax[0].axhline(balance, color="grey", lw=0.8, ls="--")
        ax[0].set_title("Each coin on its own EUR 1000 account")
        ax[0].legend(ncol=5, fontsize=8)
        ax[1].plot(pc.index, pc.values, color="black", lw=1.4)
        ax[1].axhline(balance, color="grey", lw=0.8, ls="--")
        ax[1].axvline(SPLIT, color="red", lw=0.8, ls=":")
        ax[1].text(SPLIT, ax[1].get_ylim()[1], " out-of-sample ->", color="red", va="top", fontsize=8)
        ax[1].set_title("One EUR 1000 account, all 5 coins, 1 trade at a time")
        for a_ in ax:
            a_.set_ylabel("EUR")
        fig.suptitle(f"Crypto MSB + Liq + Michael's Bands (1h, entries {p.entry_start_hour:02d}-{p.entry_end_hour:02d} Plovdiv, {p.risk_pct}% risk)")
        fig.tight_layout()
        fig.savefig(OUT / "equity.png", dpi=120)
    except ImportError:
        pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "optimize":
        optimize(sys.argv[2] if len(sys.argv) > 2 else "2")
    else:
        run()
