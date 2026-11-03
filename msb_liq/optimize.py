"""Walk-forward style robustness check.

Each dataset is split: first 60% = in-sample (IS), last 40% = out-of-sample (OOS).
Parameters are ranked ONLY on pooled IS results across all instruments
(average R per trade, needs enough trades), then the top configs are shown on
OOS data they never saw. A config is only worth trading if OOS holds up.
"""
import itertools
import sys

import numpy as np
import pandas as pd

from .data import DATASETS, costs, load
from .strategy import Params, backtest

GRID = {
    "band": [(9, 21), (12, 21), (21, 50), (50, 200)],
    "swing_len": [2, 3, 5],
    "rr": [1.0, 1.5, 2.0, 3.0],
    "msb_window": [5, 10, 20],
    "flat_hour": [23, 99],  # 23 = flat before midnight, 99 = leave SL/TP working overnight
}


def run_split(frames, p_kwargs, part):
    rs = []
    for name, df in frames.items():
        cut = int(len(df) * 0.6)
        d = df.iloc[:cut] if part == "is" else df.iloc[cut - 300:]  # 300 bars warm-up for OOS
        sp, cm = costs(name)
        res = backtest(d, Params(spread=sp, commission_pct=cm, **p_kwargs))
        if part == "oos":
            res.trades = [t for t in res.trades if t.entry_time >= df.index[cut]]
        rs += [t.r for t in res.trades]
    return np.array(rs)


def main():
    frames = {n: load(n) for n in DATASETS}
    rows = []
    for band, sw, rr, mw, fh in itertools.product(*GRID.values()):
        kw = dict(band_fast=band[0], band_slow=band[1], swing_len=sw, rr=rr, msb_window=mw, flat_hour=fh)
        r = run_split(frames, kw, "is")
        if len(r) < 40:
            continue
        rows.append({**kw, "is_trades": len(r), "is_avgR": r.mean(), "is_win": (r > 0).mean()})
    tab = pd.DataFrame(rows).sort_values("is_avgR", ascending=False)
    top = tab.head(int(sys.argv[1]) if len(sys.argv) > 1 else 15).copy()
    oos = [run_split(frames, {k: (int(row[k]) if k != "rr" else float(row[k])) for k in ("band_fast", "band_slow", "swing_len", "rr", "msb_window", "flat_hour")}, "oos")
           for _, row in top.iterrows()]
    top["oos_trades"] = [len(r) for r in oos]
    top["oos_avgR"] = [r.mean() if len(r) else np.nan for r in oos]
    top["oos_win"] = [(r > 0).mean() if len(r) else np.nan for r in oos]
    pd.set_option("display.width", 200)
    print(f"configs tested: {len(tab)} | share with positive IS avgR: {(tab.is_avgR > 0).mean():.0%}")
    print(top.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
