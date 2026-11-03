"""Loaders for the bundled H1 datasets (each file has its own format/timezone)."""
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"

# name -> (file, timezone of the timestamps, spread in price units, commission % per side)
DATASETS = {
    "EURUSD_2017_18": ("EURUSD_H1_2017_2018.csv", "UTC", 0.00010, 0.0),
    "EURUSD_2024": ("EURUSD_H1_2024.csv", "UTC", 0.00010, 0.0),
    "XAUUSD_2025": ("XAUUSD_H1_2025.csv", "Europe/Sofia", 0.30, 0.0),       # MT5 server time = EET/EEST
    "XAUUSD_2025_26": ("XAUUSD_H1_2025_2026.csv", "UTC", 0.30, 0.0),
    "BTCUSDT_2026": ("BTCUSDT_1h_2026.csv", "UTC", 0.0, 0.10),               # spot, 0.1% taker fee
}


def _std(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip("<>").lower() for c in df.columns]
    return df[["open", "high", "low", "close"]].astype(float).sort_index()


def load(name: str) -> pd.DataFrame:
    fname, tz, *_ = DATASETS[name]
    path = DATA / fname
    if name == "XAUUSD_2025":
        df = pd.read_csv(path, sep="\t")
        idx = pd.to_datetime(df["<DATE>"] + " " + df["<TIME>"], format="%Y.%m.%d %H:%M:%S")
    elif name == "BTCUSDT_2026":
        df = pd.read_csv(path)
        idx = pd.to_datetime(df["open_time_ms"], unit="ms")
    else:
        df = pd.read_csv(path)
        idx = pd.to_datetime(df.iloc[:, 0])
    df.index = idx
    df = _std(df)
    df = df[~df.index.duplicated()]
    df.index = df.index.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    return df[df.index.notna()]


def costs(name: str) -> tuple[float, float]:
    return DATASETS[name][2], DATASETS[name][3]
