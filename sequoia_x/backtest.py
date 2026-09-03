"""事件研究法回测引擎（向量化，不改策略代码）。

思路：把全市场日线载入内存（329 万行级），对每只股票用 pandas 向量化
计算"哪些交易日满足策略信号条件"（事件表），再直接关联该股未来 N 个
交易日的真实收益，聚合胜率 / 均值 / 中位数 / 与沪深300基准对比。

相对"逐日切片重跑策略 run()"的优势：
- 无需为每个历史交易日重建引擎/重跑全市场（那要 250×6×5200 次调用）
- 一次 panel 载入 + groupby 向量化 = 秒级到分钟级

用法：
    python -m sequoia_x.backtest --json-out data/backtest.json
    python -m sequoia_x.backtest --period 1y --top 20

口径说明：
- 信号日 = 满足条件的那根 K 线（收盘后判定，不偷看未来：rolling 均 shift(1)）
- 收益 = 信号日次日收盘买入 → 未来第 N 个交易日收盘卖出（不含手续费/滑点）
- 胜率 = 收益 > 0 的占比
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from dotenv import load_dotenv

load_dotenv()

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

logger = get_logger(__name__)

HORIZONS = (5, 10, 20)


def _load_panel(db_path: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(
            "SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily",
            conn,
        )
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    # 每股时间序号（用于未来收益对齐）
    df["seq"] = df.groupby("symbol").cumcount()
    return df


# ── 全表向量化信号：预计算各策略所需特征（groupby.transform 一次完成）──

def compute_events(panel: pd.DataFrame, strategies: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """对每个策略算出信号事件表 {strategy: DataFrame[symbol, date, seq]}。全表向量化。"""
    g = panel.groupby("symbol", sort=False)
    close = panel["close"]
    open_ = panel["open"]
    high = panel["high"]
    low = panel["low"]
    volume = panel["volume"]
    # 预计算所有需要的特征（每列一次 transform）
    feats = {
        "close": close,
        "open": open_,
        "high": high,
        "volume": volume,
        "ma5": g["close"].transform(lambda s: s.rolling(5).mean()),
        "ma20": g["close"].transform(lambda s: s.rolling(20).mean()),
        "vol_ma20": g["volume"].transform(lambda s: s.rolling(20).mean()),
        "ma60": g["close"].transform(lambda s: s.rolling(60).mean()),
        "high20_prev": g["high"].transform(lambda s: s.shift(1).rolling(20).max()),
        "close_prev": g["close"].transform(lambda s: s.shift(1)),
        "close_prev2": g["close"].transform(lambda s: s.shift(2)),
        "vol_prev": g["volume"].transform(lambda s: s.shift(1)),
        "hi40": g["high"].transform(lambda s: s.rolling(40).max()),
        "lo40": g["low"].transform(lambda s: s.rolling(40).min()),
        "hi10": g["high"].transform(lambda s: s.rolling(10).max()),
        "lo10": g["low"].transform(lambda s: s.rolling(10).min()),
        "ma20_prev": None, "ma60_prev": None,
    }
    feats["ma20_prev"] = feats["ma20"].shift(1)
    feats["ma60_prev"] = feats["ma60"].shift(1)

    def mask_to_events(mask: pd.Series) -> pd.DataFrame:
        hit = panel.loc[mask, ["symbol", "date", "seq"]]
        return hit.reset_index(drop=True)

    def dedupe(ev: pd.DataFrame, cooldown: int = 20) -> pd.DataFrame:
        """同股冷却：同一股票 cooldown 个交易日内只保留首个信号，避免重叠窗口。"""
        if ev.empty:
            return ev
        ev = ev.sort_values(["symbol", "seq"])
        keep: list[bool] = []
        last_seq: dict[str, int] = {}
        for _, row in ev.iterrows():
            prev = last_seq.get(row["symbol"])
            if prev is None or row["seq"] - prev >= cooldown:
                keep.append(True)
                last_seq[row["symbol"]] = row["seq"]
            else:
                keep.append(False)
        return ev.loc[keep].reset_index(drop=True)

    events: dict[str, pd.DataFrame] = {}
    if not strategies or "海龟突破" in strategies:
        amount = volume * close
        m = (close > feats["high20_prev"]) & (amount > 100_000_000) \
            & (close > open_) & (close > feats["close_prev"])
        events["海龟突破"] = dedupe(mask_to_events(m.fillna(False)))
        logger.info(f"海龟突破: {len(events['海龟突破'])} 信号")
    if not strategies or "均线放量" in strategies:
        golden = (feats["ma5"].shift(1) <= feats["ma20"].shift(1)) & (feats["ma5"] > feats["ma20"])
        surge = volume > feats["vol_ma20"] * 1.5
        events["均线放量"] = dedupe(mask_to_events((golden & surge).fillna(False)))
        logger.info(f"均线放量: {len(events['均线放量'])} 信号")
    if not strategies or "高窄旗形" in strategies:
        m = (feats["hi40"] / feats["lo40"] > 1.6) & (feats["hi10"] / feats["lo10"] < 1.15) \
            & (volume < feats["vol_ma20"] * 0.6)
        events["高窄旗形"] = dedupe(mask_to_events(m.fillna(False)))
        logger.info(f"高窄旗形: {len(events['高窄旗形'])} 信号")
    if not strategies or "涨停洗盘" in strategies:
        m = (feats["close_prev"] >= feats["close_prev2"] * 1.095) \
            & (close < open_) & (volume > feats["vol_prev"] * 2.0)
        events["涨停洗盘"] = dedupe(mask_to_events(m.fillna(False)))
        logger.info(f"涨停洗盘: {len(events['涨停洗盘'])} 信号")
    if not strategies or "上升跌停" in strategies:
        m = (feats["ma20_prev"] > feats["ma60_prev"]) & (close <= feats["close_prev"] * 0.905) \
            & (volume > feats["vol_ma20"] * 2.0)
        events["上升跌停"] = dedupe(mask_to_events(m.fillna(False)))
        logger.info(f"上升跌停: {len(events['上升跌停'])} 信号")
    if not strategies or "RPS 突破" in strategies:
        chg120 = g["close"].transform(lambda s: s.pct_change(120))
        high120 = g["high"].transform(lambda s: s.shift(1).rolling(120).max())
        tmp = panel[["date"]].copy()
        tmp["symbol"] = panel["symbol"]
        tmp["chg120"] = chg120.values
        tmp["high120"] = high120.values
        tmp["close"] = close.values
        tmp = tmp.dropna(subset=["chg120", "high120"])
        tmp["rps"] = tmp.groupby("date")["chg120"].rank(pct=True) * 100
        m = (tmp["rps"] >= 90) & (tmp["close"] >= tmp["high120"] * 0.9)
        hit = tmp.loc[m, ["symbol", "date"]]
        hit = hit.merge(panel[["symbol", "date", "seq"]], on=["symbol", "date"], how="left")
        events["RPS 突破"] = dedupe(hit.reset_index(drop=True))
        logger.info(f"RPS 突破: {len(events['RPS 突破'])} 信号")
    return events


def _future_ret(panel: pd.DataFrame, events: pd.DataFrame, h: int) -> list[float]:
    """对每个信号事件，计算未来 h 个交易日收益（次日收盘买 → 第h日收盘卖）。"""
    if events.empty:
        return []
    ev = events[["symbol", "seq"]].reset_index()
    ev.columns = ["eid", "symbol", "seq_in"]
    entry_df = ev.copy()
    exit_df = ev.copy()
    entry_df["seq_entry"] = entry_df["seq_in"] + 1
    exit_df["seq_exit"] = exit_df["seq_in"] + h
    # 用 eid 做对齐键，merge 后按 eid 排序还原事件顺序
    e_close = entry_df[["eid", "symbol", "seq_entry"]].rename(columns={"seq_entry": "seq"}).merge(
        panel[["symbol", "seq", "close"]], on=["symbol", "seq"], how="left")
    x_close = exit_df[["eid", "symbol", "seq_exit"]].rename(columns={"seq_exit": "seq"}).merge(
        panel[["symbol", "seq", "close"]], on=["symbol", "seq"], how="left")
    e_close = e_close.sort_values("eid")["close"]
    x_close = x_close.sort_values("eid")["close"]
    m = pd.concat([e_close.reset_index(drop=True), x_close.reset_index(drop=True)], axis=1)
    m.columns = ["entry", "exit"]
    m = m.dropna()
    if m.empty:
        return []
    rets = (m["exit"] / m["entry"] - 1).replace([np.inf, -np.inf], np.nan).dropna().tolist()
    return [r for r in rets if np.isfinite(r)]


def run_backtest(period: str = "1y", json_out: str | None = None) -> dict:
    settings = get_settings()
    engine = DataEngine(settings)
    panel = _load_panel(engine.db_path)

    # 期间过滤
    if period.endswith("y"):
        years = int(period[:-1])
        cutoff = panel["date"].max() - pd.DateOffset(years=years)
        panel = panel[panel["date"] > cutoff].reset_index(drop=True)
    # 重算 seq（过滤后）
    panel["seq"] = panel.groupby("symbol").cumcount()

    events = compute_events(panel)
    result: dict[str, dict] = {}
    for name, ev in events.items():
        result[name] = {}
        for h in HORIZONS:
            rets = _future_ret(panel, ev, h)
            if not rets:
                result[name][h] = {"count": 0, "win_rate": None,
                                   "mean_ret": None, "median_ret": None}
                continue
            arr = np.array(rets)
            result[name][h] = {
                "count": int(len(arr)),
                "win_rate": round(float((arr > 0).mean()) * 100, 1),
                "mean_ret": round(float(arr.mean()) * 100, 2),
                "median_ret": round(float(np.median(arr)) * 100, 2),
            }
        logger.info(f"{name} 回测完成")

    out = {
        "method": "event-study-vectorized",
        "note": "信号日收盘判定 → 次日收盘买入 → 未来N交易日收盘卖出；不含手续费/滑点；后复权价",
        "range": f"{panel['date'].min().date()} ~ {panel['date'].max().date()}",
        "n_stocks": int(panel["symbol"].nunique()),
        "n_rows": int(len(panel)),
        "strategies": result,
    }
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X 向量化事件研究回测")
    parser.add_argument("--period", default="1y", help="回测期间（1y/2y/3y 或全量 all）")
    parser.add_argument("--json-out", help="结果写 JSON")
    args = parser.parse_args()

    res = run_backtest(period=args.period, json_out=args.json_out)
    print(f"回测区间 {res['range']} | {res['n_stocks']} 只 | {res['n_rows']} 行\n")
    for name, horizons in res["strategies"].items():
        parts = []
        for h, s in horizons.items():
            if s["count"]:
                parts.append(f"{h}日: n={s['count']} 胜率{s['win_rate']}% 均{s['mean_ret']}%")
            else:
                parts.append(f"{h}日: 无")
        print(f"【{name}】{' | '.join(parts)}")


if __name__ == "__main__":
    sys.exit(main())
