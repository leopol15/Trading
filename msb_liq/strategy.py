"""MSB + Liquidity sweep + Michael's Bands strategy.

The idea (long side, short is the mirror image):

1. TREND  - "Michael's bands": a fast/slow EMA band. We only look for longs while
            the fast EMA is above the slow EMA (bands bullish) and only shorts
            while it is below.
2. LIQ    - Liquidity sweep: price wicks BELOW the most recent confirmed swing
            low (where the stop losses sit) and closes back ABOVE it. That is
            the "stop hunt" / sell-side liquidity grab.
3. MSB    - Market Structure Break: within `msb_window` bars of the sweep, a
            candle CLOSES above the most recent confirmed swing high. That is
            the confirmation that buyers took control after the sweep.
4. ENTRY  - Market order at the open of the next candle.
   STOP   - Below the sweep wick (the liquidity grab low) minus a small ATR buffer.
   TARGET - Fixed R multiple (default 2R).
5. TIME   - Signals only during the trading window (Plovdiv local time) and any
            open trade is closed before midnight, so nothing is held overnight.

Everything is computed bar-by-bar with no look-ahead: a swing point only
becomes "known" `swing_len` bars after it printed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Params:
    # Michael's bands (EMA band) - set these to whatever lengths the Prof uses
    band_fast: int = 21
    band_slow: int = 50
    # Swing / structure detection
    swing_len: int = 2          # pivot needs N lower highs/higher lows each side
    msb_window: int = 5         # bars after the sweep in which the MSB must happen
    # Risk management
    rr: float = 3.0             # take profit in R
    atr_len: int = 14
    sl_buffer_atr: float = 0.1  # extra room under/over the sweep wick
    min_risk_atr: float = 0.5   # skip setups with a tiny stop (noise)
    max_risk_atr: float = 3.0   # skip setups with a huge stop
    risk_pct: float = 1.0       # % of account risked per trade
    max_trades_per_day: int = 2
    # Session, in Europe/Sofia (Plovdiv) local time
    tz: str = "Europe/Sofia"
    entry_start_hour: int = 9   # first entry allowed at 09:00
    entry_end_hour: int = 22    # no new entries from 22:00
    flat_hour: int = 23         # close everything on the bar that opens at 23:00 (done by 00:00)
    # Costs (in price units, round trip handled by applying on entry + exit)
    spread: float = 0.0         # full spread paid on entry
    commission_pct: float = 0.0  # % of notional per side
    allow_long: bool = True
    allow_short: bool = True


@dataclass
class Trade:
    side: int
    entry_time: pd.Timestamp
    entry: float
    stop: float
    target: float
    size: float
    exit_time: pd.Timestamp | None = None
    exit: float | None = None
    reason: str = ""
    pnl: float = 0.0
    r: float = 0.0
    entry_cost: float = 0.0


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    df = df.copy()
    df["band_fast"] = ema(df["close"], p.band_fast)
    df["band_slow"] = ema(df["close"], p.band_slow)
    df["atr"] = atr(df, p.atr_len)
    n = p.swing_len
    w = 2 * n + 1
    # pivot at bar i is known at bar i+n -> shift by n so it's only usable then
    is_ph = df["high"] == df["high"].rolling(w, center=True).max()
    is_pl = df["low"] == df["low"].rolling(w, center=True).min()
    df["swing_high"] = df["high"].where(is_ph).shift(n).ffill()
    df["swing_low"] = df["low"].where(is_pl).shift(n).ffill()
    return df


@dataclass
class Result:
    trades: list[Trade]
    equity: pd.Series
    params: Params
    start_balance: float
    stats: dict = field(default_factory=dict)


def backtest(df: pd.DataFrame, p: Params, balance: float = 1000.0) -> Result:
    """df: OHLC with a tz-aware DatetimeIndex (any tz, converted to p.tz)."""
    df = add_indicators(df, p)
    local = df.index.tz_convert(p.tz)
    hours = local.hour
    days = local.date

    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    bf, bs, a = df["band_fast"].to_numpy(), df["band_slow"].to_numpy(), df["atr"].to_numpy()
    sh, sl = df["swing_high"].to_numpy(), df["swing_low"].to_numpy()

    equity = balance
    eq_curve = np.full(len(df), np.nan)
    trades: list[Trade] = []
    pos: Trade | None = None
    pending: tuple | None = None          # signal waiting for next bar open
    bull_sweep = None                     # (bar_idx, sweep_low)
    bear_sweep = None                     # (bar_idx, sweep_high)
    trades_today: dict = {}
    warmup = max(p.band_slow * 3, p.atr_len * 3, 50)

    def close_pos(i, price, reason):
        nonlocal equity, pos
        cost = pos.size * price * p.commission_pct / 100
        equity += pos.side * (price - pos.entry) * pos.size - cost
        pnl = pos.side * (price - pos.entry) * pos.size - cost - pos.entry_cost
        pos.exit_time, pos.exit, pos.reason = df.index[i], price, reason
        pos.pnl = pnl
        pos.r = pnl / (abs(pos.entry - pos.stop) * pos.size)
        trades.append(pos)
        pos = None

    for i in range(len(df)):
        # 1) fill pending entry at this bar's open
        if pending is not None and pos is None:
            side, stop, sig_i = pending
            pending = None
            entry = o[i] + side * p.spread  # pay the full spread on entry (simple + conservative)
            risk = abs(entry - stop)
            if (side == 1 and stop < entry) or (side == -1 and stop > entry):
                if p.min_risk_atr * a[i - 1] <= risk <= p.max_risk_atr * a[i - 1]:
                    size = equity * p.risk_pct / 100 / risk
                    fee = size * entry * p.commission_pct / 100
                    equity -= fee
                    pos = Trade(side, df.index[i], entry, stop, entry + side * p.rr * risk, size, entry_cost=fee)
                    trades_today[days[i]] = trades_today.get(days[i], 0) + 1

        # 2) manage open position on this bar (stop first = conservative)
        if pos is not None:
            if pos.side == 1:
                if l[i] <= pos.stop:
                    close_pos(i, min(pos.stop, o[i]), "stop")
                elif h[i] >= pos.target:
                    close_pos(i, max(pos.target, o[i]), "target")
            else:
                if h[i] >= pos.stop:
                    close_pos(i, max(pos.stop, o[i]), "stop")
                elif l[i] <= pos.target:
                    close_pos(i, min(pos.target, o[i]), "target")
            if pos is not None and p.flat_hour < 24 and (hours[i] >= p.flat_hour or hours[i] < p.entry_start_hour):
                close_pos(i, c[i], "session_end")

        eq_curve[i] = equity + (pos.side * (c[i] - pos.entry) * pos.size if pos else 0)
        if i < warmup or np.isnan(sh[i]) or np.isnan(sl[i]):
            continue

        # 3) track liquidity sweeps (computed on every bar so state stays honest)
        if l[i] < sl[i] and c[i] > sl[i]:
            bull_sweep = (i, l[i]) if bull_sweep is None or i - bull_sweep[0] > p.msb_window or l[i] < bull_sweep[1] else bull_sweep
        if h[i] > sh[i] and c[i] < sh[i]:
            bear_sweep = (i, h[i]) if bear_sweep is None or i - bear_sweep[0] > p.msb_window or h[i] > bear_sweep[1] else bear_sweep
        if bull_sweep and i - bull_sweep[0] > p.msb_window:
            bull_sweep = None
        if bear_sweep and i - bear_sweep[0] > p.msb_window:
            bear_sweep = None

        # 4) MSB after a sweep -> signal
        in_window = p.entry_start_hour <= hours[i] + 1 < p.entry_end_hour  # entry happens next bar
        can_trade = pos is None and in_window and trades_today.get(days[i], 0) < p.max_trades_per_day
        if p.allow_long and bull_sweep and i > bull_sweep[0] and c[i] > sh[i] and c[i - 1] <= sh[i]:
            if can_trade and bf[i] > bs[i]:
                pending = (1, bull_sweep[1] - p.sl_buffer_atr * a[i], i)
            bull_sweep = None
        elif p.allow_short and bear_sweep and i > bear_sweep[0] and c[i] < sl[i] and c[i - 1] >= sl[i]:
            if can_trade and bf[i] < bs[i]:
                pending = (-1, bear_sweep[1] + p.sl_buffer_atr * a[i], i)
            bear_sweep = None

    if pos is not None:
        close_pos(len(df) - 1, c[-1], "end_of_data")
        eq_curve[-1] = equity

    res = Result(trades, pd.Series(eq_curve, index=df.index).ffill(), p, balance)
    res.stats = stats(res)
    return res


def stats(res: Result) -> dict:
    t = res.trades
    eq = res.equity.dropna()
    if not t:
        return {"trades": 0, "net_profit": 0.0, "return_pct": 0.0}
    pnl = np.array([x.pnl for x in t])
    rs = np.array([x.r for x in t])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    dd = (eq / eq.cummax() - 1).min() * 100
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    end = res.start_balance + pnl.sum()
    return {
        "trades": len(t),
        "win_rate_pct": round(len(wins) / len(t) * 100, 1),
        "avg_R": round(rs.mean(), 3),
        "expectancy_eur": round(pnl.mean(), 2),
        "profit_factor": round(pf, 2),
        "net_profit": round(pnl.sum(), 2),
        "end_balance": round(end, 2),
        "return_pct": round((end / res.start_balance - 1) * 100, 2),
        "max_drawdown_pct": round(dd, 2),
        "longs": int(sum(x.side == 1 for x in t)),
        "shorts": int(sum(x.side == -1 for x in t)),
        "exits": {k: int(sum(x.reason == k for x in t)) for k in ("target", "stop", "session_end", "end_of_data")},
    }
