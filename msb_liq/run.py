"""Run the strategy on every bundled dataset and write a report.

    python -m msb_liq.run                 # all datasets, EUR 1000, 1% risk
    python -m msb_liq.run --risk 0.5      # half the risk per trade
"""
import argparse
from pathlib import Path

import pandas as pd

from .data import DATASETS, costs, load
from .strategy import Params, backtest

OUT = Path(__file__).resolve().parent.parent / "results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--risk", type=float, default=1.0, help="%% of balance risked per trade")
    ap.add_argument("--hold-overnight", action="store_true", help="leave SL/TP working after midnight")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    rows, curves = [], {}
    for name in DATASETS:
        sp, cm = costs(name)
        p = Params(spread=sp, commission_pct=cm, risk_pct=args.risk, flat_hour=99 if args.hold_overnight else 23)
        res = backtest(load(name), p, args.balance)
        s = res.stats
        rows.append({"dataset": name, **{k: v for k, v in s.items() if k != "exits"}, **{f"exit_{k}": v for k, v in s["exits"].items()}})
        curves[name] = res.equity.reset_index(drop=True)
        pd.DataFrame([t.__dict__ for t in res.trades]).to_csv(OUT / f"trades_{name}.csv", index=False)

    tab = pd.DataFrame(rows)
    tab.to_csv(OUT / "summary.csv", index=False)
    pd.set_option("display.width", 220)
    print(tab.to_string(index=False))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 5))
        for n, c in curves.items():
            ax.plot(c.values, label=n, lw=1.4)
        ax.axhline(args.balance, color="grey", lw=0.8, ls="--")
        ax.set_title(f"MSB + Liq + Michael's Bands  |  EUR {args.balance:.0f} start, {args.risk}% risk/trade")
        ax.set_xlabel("H1 bars")
        ax.set_ylabel("Equity (EUR)")
        ax.legend()
        fig.tight_layout()
        fig.savefig(OUT / "equity.png", dpi=120)
        print(f"\nsaved {OUT/'equity.png'}")
    except ImportError:
        print("\n(matplotlib not installed - skipped chart)")


if __name__ == "__main__":
    main()
