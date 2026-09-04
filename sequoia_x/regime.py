"""市场风格过滤器（Regime Filter）：给策略信号打市场状态标签。

动机（P1 研究结论）：高窄旗形 2y +18.3% 但同期沪深300 +40%，超额 -21.7%——
单一形态策略是风格依赖 beta 而非稳定 alpha。本模块把市场分成状态，
供事件研究分状态统计 / 组合模拟按状态开关 / 日报提示当前风格。

状态定义（日频，收盘后判定，无前视）：
- trend：沪深300 收盘 vs MA200 → 'up'（站上，多头） / 'down'（下方，空头）
  MA200 用指数日线，非交易日沿用最近值
- vol：沪深300 近 20 日年化波动率 vs 滚动 1y 中位 → 'low' / 'high'
- regime = trend × vol 组合（如 up_low = 最宜做多的顺风状态）

数据：本地库 index_daily 表（baostock sh.000300 日线，首次自动拉取缓存）。
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

INDEX_CODE = "sh.000300"
INDEX_NAME = "沪深300"
_INDEX_TABLE = """
CREATE TABLE IF NOT EXISTS index_daily (
    code   TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL,
    UNIQUE (code, date)
);
"""


def _fetch_index_history(db_path: str, start: str = "2023-01-01") -> int:
    """从 baostock 拉沪深300 日线入本地 index_daily（增量）。返回写入行数。"""
    import baostock as bs

    with sqlite3.connect(db_path) as conn:
        conn.execute(_INDEX_TABLE)
        row = conn.execute(
            "SELECT MAX(date) FROM index_daily WHERE code = ?", (INDEX_CODE,)
        ).fetchone()
    last = row[0] if row and row[0] else start
    end = date.today().isoformat()
    if last >= end:
        return 0

    lg = bs.login()
    if lg.error_code != "0":
        logger.error(f"baostock 登录失败: {lg.error_msg}")
        return 0
    try:
        rs = bs.query_history_k_data_plus(
            INDEX_CODE, "date,close", start_date=last, end_date=end,
            frequency="d", adjustflag="3",
        )
        rows = []
        while rs.next():
            r = rs.get_row_data()
            rows.append([INDEX_CODE, r[0], float(r[1])])
    finally:
        bs.logout()
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=["code", "date", "close"])
    df = df.dropna()
    with sqlite3.connect(db_path) as conn:
        keys = df[["code", "date"]].values.tolist()
        conn.executemany("DELETE FROM index_daily WHERE code = ? AND date = ?", keys)
        df.to_sql("index_daily", conn, if_exists="append", index=False)
        conn.commit()
    logger.info(f"沪深300 指数更新 {len(df)} 行")
    return len(df)


def get_market_states(db_path: str, refresh: bool = True) -> pd.DataFrame:
    """返回 date → market_state 序列（DataFrame: date, trend, vol, regime）。

    refresh=True 时先尝试增量拉指数；失败/无网则用本地已有数据。
    """
    if refresh:
        try:
            _fetch_index_history(db_path)
        except Exception as exc:
            logger.warning(f"沪深300 刷新失败（用本地）：{exc}")
    with sqlite3.connect(db_path) as conn:
        conn.execute(_INDEX_TABLE)
        df = pd.read_sql(
            "SELECT date, close FROM index_daily WHERE code = ? ORDER BY date",
            conn, params=(INDEX_CODE,),
        )
    if df.empty:
        logger.warning("本地无沪深300 数据，regime filter 不可用")
        return pd.DataFrame(columns=["date", "trend", "vol", "regime"])
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")["close"]

    # 趋势：收盘 vs MA200
    ma200 = s.rolling(200).mean()
    trend = pd.Series(
        ["up" if v > m else "down" for v, m in zip(s, ma200)],
        index=s.index,
    )
    trend[ma200.isna()] = np.nan
    # 波动率：20 日年化 vs 滚动 252 日中位
    vol20 = s.pct_change().rolling(20).std() * np.sqrt(252)
    vol_base = vol20.rolling(252).median()
    vol = pd.Series(
        ["low" if (not np.isnan(v) and not np.isnan(b) and v <= b) else "high"
         for v, b in zip(vol20, vol_base)],
        index=s.index,
    )
    vol[(vol20.isna()) | (vol_base.isna())] = np.nan
    regime = pd.Series(
        [(t if not pd.isna(t) else "na") + "_" + (v if not pd.isna(v) else "na")
         for t, v in zip(trend, vol)],
        index=s.index,
    )
    out = pd.DataFrame({"date": s.index, "trend": trend.values,
                        "vol": vol.values, "regime": regime.values})
    out = out.dropna(subset=["trend", "vol"])
    return out.reset_index(drop=True)


def regime_for_dates(states: pd.DataFrame, dates: pd.Series) -> pd.Series:
    """给任意日期序列打市场状态（向前取最近一个已知状态，防未来函数）。"""
    s = states.set_index("date")["regime"].sort_index()
    # 每个查询日期：取 <= 该日期的最后一个状态（asof 前向无泄漏）
    idx = pd.to_datetime(pd.Series(dates)).reset_index(drop=True)
    mapped = s.reindex(idx, method="ffill")
    return mapped.reset_index(drop=True)


def label_events(states: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """给事件表加 market_state 列（信号日市场状态）。"""
    ev = events.copy()
    if ev.empty:
        ev["market_state"] = pd.Series(dtype=str)
        return ev
    ev = ev.sort_values("date").reset_index(drop=True)
    st = states.set_index("date")["regime"].sort_index()
    mapped = st.reindex(pd.to_datetime(ev["date"]), method="ffill")
    ev["market_state"] = mapped.values
    return ev
